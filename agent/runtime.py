"""Runtime do agente: loop de tool calling com Gemma 4 31B via OpenRouter."""
import json
import logging
import os

import httpx

import db
from agent import prompts, registry, router as nlu_router, tools  # noqa: F401 (registra tools)

log = logging.getLogger("agent")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_CALLS_PER_TURN = 8
HISTORY_WINDOW = 20      # mantém últimas N mensagens em memória
SUMMARIZE_AT = 30        # quando passa disso, comprime as antigas


def _chat_model() -> str:
    return os.environ.get("OPENROUTER_CHAT_MODEL", "deepseek/deepseek-chat-v3.1:free")


async def get_or_create_conversation(user_id: int) -> dict:
    """Pega conversa ativa ou cria uma. Por enquanto sempre uma por user."""
    pool = db.pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from conversations where user_id=$1 and state='active' order by last_at desc limit 1",
            user_id,
        )
        if row:
            return dict(row)
        row = await conn.fetchrow(
            "insert into conversations (user_id) values ($1) returning *", user_id
        )
        return dict(row)


async def add_message(
    conv_id: int, role: str, content: str | None = None,
    tool_calls: list | None = None, tool_call_id: str | None = None,
    photo_file_id: str | None = None,
) -> None:
    pool = db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into messages (conversation_id, role, content, tool_calls, tool_call_id, photo_file_id)
            values ($1,$2,$3,$4::jsonb,$5,$6)
            """,
            conv_id, role, content,
            json.dumps(tool_calls) if tool_calls else None,
            tool_call_id, photo_file_id,
        )
        await conn.execute(
            "update conversations set last_at=now() where id=$1", conv_id
        )


async def _load_messages(conv_id: int) -> list[dict]:
    pool = db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select role, content, tool_calls, tool_call_id
            from messages where conversation_id=$1
            order by created_at desc limit $2
            """,
            conv_id, HISTORY_WINDOW,
        )
    rows = list(reversed(rows))
    out = []
    for r in rows:
        msg = {"role": r["role"]}
        if r["content"] is not None:
            msg["content"] = r["content"]
        if r["tool_calls"]:
            msg["tool_calls"] = json.loads(r["tool_calls"]) if isinstance(r["tool_calls"], str) else r["tool_calls"]
        if r["tool_call_id"]:
            msg["tool_call_id"] = r["tool_call_id"]
        out.append(msg)
    return out


async def _last_photo_id(conv_id: int) -> str | None:
    """Última photo_file_id que veio nessa conversa (das últimas 5 msgs)."""
    pool = db.pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select photo_file_id from messages
            where conversation_id=$1 and photo_file_id is not null
            order by created_at desc limit 1
            """,
            conv_id,
        )
    return row["photo_file_id"] if row else None


async def _load_summary(conv_id: int) -> str | None:
    pool = db.pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("select summary from conversations where id=$1", conv_id)


class OpenRouterError(RuntimeError):
    """Erro estruturado do OpenRouter. status_code=0 quando a resposta veio
    'OK' (HTTP 200) mas não tinha 'choices' válidos (rate-limit, content filter,
    provider offline retornando JSON sem o formato esperado, etc)."""
    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _user_friendly_openrouter_error(err: OpenRouterError) -> str:
    """Converte OpenRouterError em mensagem amigável pro user final."""
    sc = err.status_code
    if sc == 401:
        return "Chave da OpenRouter inválida ou revogada. Verifica `OPENROUTER_API_KEY` no .env."
    if sc == 402:
        return "Sem créditos na OpenRouter. Recarrega em openrouter.ai/credits."
    if sc == 403:
        return "Modelo bloqueado (privacy/content filter). Tenta outro modelo ou ajusta em openrouter.ai/settings/privacy."
    if sc == 429:
        return "Rate limit da OpenRouter — tenta de novo em alguns segundos."
    if sc >= 500:
        return "OpenRouter ou o provedor do modelo está fora do ar. Tenta de novo em ~1min."
    return "Modelo indisponível agora. Tenta de novo, ou troca o modelo (/modelo)."


async def _post_openrouter(payload: dict) -> dict:
    """Chama OpenRouter e valida resposta. Levanta OpenRouterError em falha."""
    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/local/health-chat",
        "X-Title": "health-chat",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        body_preview = r.text[:500]
        if r.status_code >= 400:
            log.warning("OpenRouter HTTP %s: %s", r.status_code, body_preview)
            raise OpenRouterError(
                f"OpenRouter {r.status_code}", status_code=r.status_code, body=r.text,
            )
        try:
            data = r.json()
        except Exception as e:
            log.warning("OpenRouter resposta não-JSON: %s", body_preview)
            raise OpenRouterError(
                f"resposta inválida: {e}", status_code=r.status_code, body=r.text,
            )
        # Validação de formato — alguns providers retornam HTTP 200 com {"error":...}
        # em vez de erro HTTP, e às vezes 'choices' vem vazio ou ausente.
        if isinstance(data, dict) and data.get("error"):
            err_info = data["error"]
            msg = err_info.get("message") if isinstance(err_info, dict) else str(err_info)
            log.warning("OpenRouter erro embutido (HTTP %s): %s", r.status_code, msg)
            raise OpenRouterError(
                f"erro do provedor: {msg}", status_code=r.status_code, body=r.text,
            )
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            log.warning("OpenRouter sem 'choices' (HTTP %s): %s", r.status_code, body_preview)
            raise OpenRouterError(
                "resposta sem 'choices'", status_code=r.status_code, body=r.text,
            )
        return data


# Mapa de tool → label humano pra mostrar como progresso enquanto roda.
# Tools fora desse mapa caem no fallback "⚙️ {name}".
_TOOL_PROGRESS_LABELS = {
    # Visão
    "estimate_meal_from_photo": "📷 Analisando o prato",
    "parse_menu_image": "📷 Lendo o cardápio",
    "parse_menu_photo": "📷 Lendo o cardápio",
    "parse_watch_photo": "📷 Lendo o relógio",
    "parse_scale_photo": "📷 Lendo a balança",
    # Search / match
    "search_foods": "🔍 Buscando alimentos",
    "search_vitat": "🌿 Consultando Vitat",
    "fetch_vitat_food": "🌿 Consultando Vitat",
    "get_food_portions": "🔍 Conferindo porções",
    # Log
    "log_meal": "📝 Registrando refeição",
    "log_template": "📝 Registrando refeição",
    "log_weight": "⚖️ Salvando peso",
    "log_exercise": "🏃 Registrando treino",
    # Profile / goal
    "set_profile": "🎯 Atualizando perfil",
    "compute_daily_goal": "🎯 Calculando meta",
    "compare_to_goal": "🎯 Comparando com meta",
    # Read
    "get_today_summary": "📊 Consultando o dia",
    "get_calorie_balance": "📊 Calculando saldo",
    "get_recent_meals": "📊 Buscando refeições",
    "get_period_summary": "📊 Resumindo período",
    # Charts
    "generate_daily_chart": "📊 Gerando gráfico",
    "generate_weight_chart": "📊 Gerando gráfico de peso",
    "generate_report_chart": "📊 Gerando relatório",
    # Other
    "remember": "💭 Anotando",
}


def _tool_progress_label(tool_name: str) -> str:
    return _TOOL_PROGRESS_LABELS.get(tool_name, f"⚙️ {tool_name}")


async def run_turn(user_id: int, user_text: str, photo_file_id: str | None,
                   download_photo, vision_model: str,
                   on_progress=None) -> str:
    """Executa um turno completo. Retorna o texto pra mandar ao usuário.

    on_progress: callable opcional (async ou sync) `on_progress(label: str)`
        chamado quando uma nova tool começa a executar. Permite ao adapter
        (Telegram) mostrar 'pensando…' com etapa atual. Tolerante a falha.
    """
    conv = await get_or_create_conversation(user_id)
    conv_id = conv["id"]

    # 1. salva a mensagem do user (com marcador de foto se houver)
    user_content = user_text or ""
    if photo_file_id:
        marker = f"[Foto anexada: {photo_file_id}]"
        user_content = f"{marker}\n{user_content}".strip()
    await add_message(conv_id, "user", content=user_content, photo_file_id=photo_file_id)

    # 1.5 NLU determinística: classifica intent + restringe tool set
    # Baixa foto UMA vez aqui (router precisa, tools reaproveitam via ctx)
    photo_bytes: bytes | None = None
    if photo_file_id:
        try:
            photo_bytes = await download_photo(photo_file_id)
        except Exception:
            log.exception("falha baixando foto pro router")
    history_for_router = await _load_messages(conv_id)
    decision = await nlu_router.classify(
        text=user_text or "",
        photo_bytes=photo_bytes,
        history=history_for_router,
        vision_model=vision_model,
    )

    # 2. monta payload base (com addendum do router quando aplicável)
    summary = await _load_summary(conv_id)
    system_blocks = [prompts.SYSTEM_PROMPT]
    if decision.system_addendum:
        system_blocks.append(decision.system_addendum)
    # Pré-carrega templates de refeição do user (até 10 mais usados)
    try:
        templates = await db.list_meal_templates(user_id, limit=10)
        if templates:
            lines = ["[Refeições salvas do usuário — use log_template pra logar:]"]
            for t in templates:
                tot = t["totals"]
                lines.append(
                    f"  • {t['name']} — {tot.get('kcal',0):.0f} kcal "
                    f"(P {tot.get('protein_g',0):.0f}g)"
                )
            system_blocks.append("\n".join(lines))
    except Exception:
        log.exception("falha carregando templates")
    if summary:
        system_blocks.append(f"\n[Resumo da conversa anterior]\n{summary}")
    system_msg = {"role": "system", "content": "\n".join(system_blocks)}

    ctx = {
        "user_id": user_id,
        "conv_id": conv_id,
        "download_photo": download_photo,
        "vision_model": vision_model,
        # Foto desta msg (ou da última, recuperada do DB se nesta não veio)
        "latest_photo_id": photo_file_id or await _last_photo_id(conv_id),
        # Cache: tools de visão reaproveitam pra evitar re-download
        "latest_photo_bytes": photo_bytes,
        # Roteamento (debug / log)
        "routing_decision": decision,
    }

    # 3. loop de tool calling
    final_text: str | None = None
    tool_history: list[tuple[str, dict, dict]] = []  # (name, args, result) — pra fallback summary
    for step in range(MAX_TOOL_CALLS_PER_TURN + 1):
        history = await _load_messages(conv_id)
        messages = [system_msg] + history

        # Se estoura o limite, força resposta final SEM tools no payload
        force_final = step >= MAX_TOOL_CALLS_PER_TURN
        payload: dict = {
            "model": _chat_model(),
            "messages": messages,
            "temperature": 0.3,
        }
        if not force_final:
            payload["tools"] = registry.openai_schema(decision.allowed_tools)
            payload["tool_choice"] = "auto"
        else:
            # Injeta instrução pra fechar
            messages.append({
                "role": "system",
                "content": "[Limite de tool calls atingido. RESPONDA AGORA ao usuário em texto, com o que você já sabe. Não chame mais tools.]"
            })

        try:
            raw = await _post_openrouter(payload)
        except OpenRouterError as e:
            log.warning("OpenRouter falhou no step %s: %s", step, e)
            # Se já tivemos tool calls bem-sucedidas neste turno, sintetizamos
            # uma resposta com o que rodou; senão, mensagem amigável.
            if tool_history:
                final_text = _synthesize_fallback_text(tool_history)
            else:
                final_text = _user_friendly_openrouter_error(e)
            break
        except Exception as e:
            log.exception("OpenRouter call falhou (não-OpenRouterError)")
            final_text = f"Erro chamando o modelo: {e}"
            break

        try:
            choice = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            log.error("OpenRouter resposta com formato inesperado: %r", raw)
            final_text = "Modelo retornou resposta vazia. Tenta de novo."
            break
        tool_calls = choice.get("tool_calls") or []
        text = choice.get("content")

        # salva a resposta do assistant (texto e/ou tool_calls)
        await add_message(
            conv_id, "assistant",
            content=text if text else None,
            tool_calls=tool_calls if tool_calls else None,
        )

        if not tool_calls:
            final_text = text
            break

        # executa cada tool e salva resultado
        for tc in tool_calls:
            tc_id = tc.get("id") or f"call_{step}"
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                args = {}
            log.info("[agent] tool: %s(%s)", name, args)
            # Sinaliza progresso pro adapter (Telegram edita o placeholder)
            if on_progress is not None:
                try:
                    res = on_progress(_tool_progress_label(name))
                    if hasattr(res, "__await__"):
                        await res
                except Exception:
                    log.debug("on_progress callback falhou (não-crítico)", exc_info=True)
            try:
                result = await registry.call(name, args, ctx)
            except Exception as e:
                log.exception("tool %s falhou", name)
                result = {"error": str(e), "hint": "Tente outra abordagem ou ferramenta."}
            tool_history.append((name, args, result))
            result_str = json.dumps(result, ensure_ascii=False, default=str)
            if len(result_str) > 8000:
                result_str = result_str[:8000] + '..."<truncated>"'
            await add_message(conv_id, "tool", content=result_str, tool_call_id=tc_id)

    # Fallback final: se LLM nunca produziu texto, sintetiza um a partir do que fez.
    if not final_text:
        final_text = _synthesize_fallback_text(tool_history)
    return final_text


def _synthesize_fallback_text(tool_history: list[tuple[str, dict, dict]]) -> str:
    """Quando o modelo executa ações mas esquece de escrever texto final,
    montamos uma confirmação a partir do que realmente aconteceu."""
    if not tool_history:
        return "(sem resposta)"

    # Procura a última ação confirmadora bem-sucedida
    for name, _args, result in reversed(tool_history):
        if not isinstance(result, dict) or result.get("error"):
            continue
        if name == "log_meal":
            totals = result.get("totals", {})
            kcal = totals.get("kcal", 0)
            p = totals.get("protein_g", 0)
            mid = result.get("meal_id", "?")
            return (f"✅ Refeição #{mid} logada: <b>{kcal:.0f} kcal</b> "
                    f"({p:.0f}g proteína).")
        if name == "log_exercise":
            return (f"✅ Exercício registrado: <b>{result.get('activity','?')}</b> "
                    f"— {result.get('kcal_burned','?')} kcal queimadas.")
        if name == "log_weight":
            gu = result.get("goal_update") or {}
            base = f"✅ Peso {result.get('weight_kg','?')} kg registrado."
            if gu.get("new_daily_kcal"):
                base += (f" Nova meta calórica: <b>{gu['new_daily_kcal']} kcal/dia</b>"
                         + (f" (Δ {gu.get('delta_kcal',0):+d})" if gu.get("delta_kcal") else "")
                         + ".")
            return base
        if name == "set_profile":
            return f"✅ {result.get('field','?')} salvo: {result.get('value','?')}."
        if name == "compute_daily_goal":
            return (f"🎯 Meta calculada: <b>{result.get('daily_kcal','?')} kcal/dia</b> "
                    f"— {result.get('protein_g','?')}g proteína, "
                    f"{result.get('carbs_g','?')}g carbo, "
                    f"{result.get('fat_g','?')}g gordura.")

    # Nenhuma ação reconhecível — mostra fallback genérico
    done = ", ".join(n for n, _, _ in tool_history[-4:])
    return ("Processei sua mensagem mas o modelo não fechou com texto. "
            f"Ferramentas chamadas: <code>{done}</code>. "
            "Tenta reformular se faltar algo.")
