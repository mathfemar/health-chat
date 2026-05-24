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


async def _post_openrouter(payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/local/health-chat",
        "X-Title": "health-chat",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenRouter {r.status_code}: {r.text}")
        return r.json()


async def run_turn(user_id: int, user_text: str, photo_file_id: str | None,
                   download_photo, vision_model: str,
                   on_stage=None) -> str:
    """Executa um turno completo. Retorna o texto pra mandar ao usuário.

    on_stage: callable opcional (async ou sync). Recebe string identificando
    o estágio atual — usado pra mostrar 'Pensando...', 'Buscando alimento...',
    etc. ao usuário. Valores possíveis:
      'downloading_photo' | 'routing' | 'thinking' | 'thinking_more' |
      'finalizing' | 'tool:<nome_da_tool>'
    Erros do callback são engolidos (não-críticos).
    """
    async def _stage(name: str) -> None:
        if on_stage is None:
            return
        try:
            res = on_stage(name)
            if hasattr(res, "__await__"):
                await res
        except Exception:
            log.debug("on_stage(%r) raised — ignorado", name, exc_info=True)

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
        await _stage("downloading_photo")
        try:
            photo_bytes = await download_photo(photo_file_id)
        except Exception:
            log.exception("falha baixando foto pro router")
    await _stage("routing")
    history_for_router = await _load_messages(conv_id)
    # Lê estado pendente (proposta de refeição/etc) pra router decidir certo
    pending = await db.get_pending_proposal(conv_id)
    pending_kind = pending.get("kind") if pending else None
    decision = await nlu_router.classify(
        text=user_text or "",
        photo_bytes=photo_bytes,
        history=history_for_router,
        vision_model=vision_model,
        pending_kind=pending_kind,
    )

    # 2. monta payload base (com addendum do router quando aplicável)
    summary = await _load_summary(conv_id)
    system_blocks = [prompts.SYSTEM_PROMPT]
    if decision.system_addendum:
        system_blocks.append(decision.system_addendum)
    # Se há proposta pendente, exponha pro LLM (assim ele consegue responder
    # ajustes textuais tipo "muda pra 150g" sem se perder)
    if pending:
        system_blocks.append(_format_pending_block(pending))
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

        # Sinal pro user: 'pensando' (step 0) ou 'pensando mais' (subsequentes)
        # ou 'finalizando' (último turno forçado)
        if force_final:
            await _stage("finalizing")
        elif step == 0:
            await _stage("thinking")
        else:
            await _stage("thinking_more")

        try:
            raw = await _post_openrouter(payload)
        except Exception as e:
            log.exception("OpenRouter call falhou")
            final_text = f"Erro chamando o modelo: {e}"
            break

        choice = raw["choices"][0]["message"]
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
            await _stage(f"tool:{name}")
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


def _format_pending_block(p: dict) -> str:
    """Resumo da proposta pendente, injetado no system prompt do turno."""
    kind = p.get("kind", "?")
    if kind == "meal":
        totals = p.get("totals") or {}
        items = p.get("items") or []
        lines = [
            "[Proposta pendente — kind=meal] Já existe uma refeição AGUARDANDO confirmação.",
            f"  Total: {totals.get('kcal', 0):.0f} kcal · P {totals.get('protein_g', 0):.0f}g",
            f"  Itens: {len(items)} — " + ", ".join(
                f"{it.get('name_llm', '?')} {it.get('portion_g', 0):.0f}g"
                for it in items[:4]
            ),
        ]
        if p.get("eaten_at_iso"):
            lines.append(f"  Comida em: {p['eaten_at_iso']}")
        lines.append(
            "REGRAS quando há proposta pendente:\n"
            "  • Se user confirmou ('sim/loga/ok') → chame confirm_proposal()\n"
            "  • Se user cancelou ('não/cancela') → chame cancel_proposal()\n"
            "  • Se user pediu ajuste ('muda pra 150g', 'adiciona X', 'tira Y') →\n"
            "    chame propose_meal() de novo com os items corrigidos (sobrescreve).\n"
            "  • Se user mudou de assunto → ignore a proposta (ela continua pendente)."
        )
        return "\n".join(lines)
    return f"[Proposta pendente — kind={kind}]"


def _synthesize_fallback_text(tool_history: list[tuple[str, dict, dict]]) -> str:
    """Quando o modelo executa ações mas esquece de escrever texto final,
    montamos uma confirmação a partir do que realmente aconteceu."""
    if not tool_history:
        return "(sem resposta)"

    # Procura a última ação confirmadora bem-sucedida
    for name, _args, result in reversed(tool_history):
        if not isinstance(result, dict) or result.get("error"):
            continue
        if name in ("log_meal", "confirm_proposal"):
            totals = result.get("totals", {})
            kcal = totals.get("kcal", 0)
            p = totals.get("protein_g", 0)
            mid = result.get("meal_id", "?")
            return (f"✅ Refeição #{mid} logada: <b>{kcal:.0f} kcal</b> "
                    f"({p:.0f}g proteína).")
        if name == "propose_meal":
            totals = result.get("totals", {})
            return (f"Proposta criada: <b>{totals.get('kcal', 0):.0f} kcal</b> "
                    f"({totals.get('protein_g', 0):.0f}g proteína). "
                    "Confirma logar?")
        if name == "cancel_proposal":
            return "Ok, cancelei."
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
