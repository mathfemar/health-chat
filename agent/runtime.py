"""Runtime do agente: loop de tool calling com Gemma 4 31B via OpenRouter."""
import json
import logging
import os

import httpx

import db
from agent import prompts, registry, tools  # noqa: F401 (registra tools)

log = logging.getLogger("agent")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_CALLS_PER_TURN = 8
HISTORY_WINDOW = 20      # mantém últimas N mensagens em memória
SUMMARIZE_AT = 30        # quando passa disso, comprime as antigas


def _chat_model() -> str:
    return os.environ.get("OPENROUTER_CHAT_MODEL", "google/gemma-4-31b-it")


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
                   bot, vision_model: str) -> str:
    """Executa um turno completo. Retorna o texto pra mandar ao usuário."""
    conv = await get_or_create_conversation(user_id)
    conv_id = conv["id"]

    # 1. salva a mensagem do user (com marcador de foto se houver)
    user_content = user_text or ""
    if photo_file_id:
        marker = f"[Foto anexada: {photo_file_id}]"
        user_content = f"{marker}\n{user_content}".strip()
    await add_message(conv_id, "user", content=user_content, photo_file_id=photo_file_id)

    # 2. monta payload base
    summary = await _load_summary(conv_id)
    system_blocks = [prompts.SYSTEM_PROMPT]
    if summary:
        system_blocks.append(f"\n[Resumo da conversa anterior]\n{summary}")
    system_msg = {"role": "system", "content": "\n".join(system_blocks)}

    ctx = {
        "user_id": user_id,
        "conv_id": conv_id,
        "bot": bot,
        "vision_model": vision_model,
    }

    # 3. loop de tool calling
    final_text: str | None = None
    for step in range(MAX_TOOL_CALLS_PER_TURN + 1):
        history = await _load_messages(conv_id)
        messages = [system_msg] + history
        payload = {
            "model": _chat_model(),
            "messages": messages,
            "tools": registry.openai_schema(),
            "tool_choice": "auto",
            "temperature": 0.3,
        }

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
            final_text = text or "(sem resposta)"
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
            try:
                result = await registry.call(name, args, ctx)
            except Exception as e:
                log.exception("tool %s falhou", name)
                result = {"error": str(e), "hint": "Tente outra abordagem ou ferramenta."}
            # tool result: serializa, limita tamanho
            result_str = json.dumps(result, ensure_ascii=False, default=str)
            if len(result_str) > 8000:
                result_str = result_str[:8000] + '..."<truncated>"'
            await add_message(conv_id, "tool", content=result_str, tool_call_id=tc_id)

        if step >= MAX_TOOL_CALLS_PER_TURN - 1:
            # forçar uma resposta final na próxima iter
            await add_message(
                conv_id, "system",
                content="[Limite de chamadas atingido — responda ao usuário com o que você já sabe]",
            )

    return final_text or "(turno sem texto final)"
