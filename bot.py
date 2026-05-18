import html
import io
import json
import logging
import os
import re
from datetime import timezone

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import calculator
import commands
import db
import llm
import matcher
from agent import runtime as agent

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("health-chat")

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"]) if os.getenv("ALLOWED_USER_ID") else None


# ============================================================
# Reply Keyboard — botões persistentes embaixo do chat
# ============================================================
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🍽 Refeição"), KeyboardButton("⚖️ Peso"), KeyboardButton("🏃 Treino")],
        [KeyboardButton("📊 Hoje"), KeyboardButton("🎯 Meta"), KeyboardButton("⚙️ Mais")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MORE_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📈 Relatório"), KeyboardButton("⏰ Lembrete")],
        [KeyboardButton("🔍 Buscar"), KeyboardButton("❓ Ajuda")],
        [KeyboardButton("🔄 Nova conversa"), KeyboardButton("⬅️ Voltar")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def _esc(s) -> str:
    return html.escape(str(s))


def _md_to_html(text: str) -> str:
    """Converte markdown comum (que LLM solta apesar do prompt) pra HTML do Telegram.
    Lida com **bold**, *italic*, `code` e sanitiza HTML quebrado (tags órfãs).
    """
    # remove separadores estilo "---" ou "===" sozinhos numa linha
    text = re.sub(r"^\s*(?:[-=]{3,})\s*$", "", text, flags=re.MULTILINE)
    # tabelas markdown
    text = re.sub(r"^\s*\|[\s:|-]+\|\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|(.+)\|\s*$",
                  lambda m: "  " + "  ".join(c.strip() for c in m.group(1).split("|") if c.strip()),
                  text, flags=re.MULTILINE)
    # **bold** → <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # *italic*
    text = re.sub(r"(?<!\w)\*([^\s*][^*]*?)\*(?!\w)", r"<i>\1</i>", text)
    # `code` → <code>code</code>
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)
    # Múltiplas quebras viram no máximo 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Normaliza malformações comuns do LLM (<tag/>, <<tag>, <//tag>)
    text = _normalize_llm_html_garbage(text)
    # Sanitiza HTML: balanceia open/close, escapa < > órfãos
    text = _balance_html_tags(text)
    return text.strip()


_ALLOWED_TG_TAGS = {"b", "i", "u", "s", "code", "pre", "strong", "em"}


_CLEAN_TAG_RE = re.compile(
    r"<(/?)(" + "|".join(_ALLOWED_TG_TAGS) + r")(\s+[^<>]*?)?(/?)>",
    re.IGNORECASE,
)


def _normalize_llm_html_garbage(text: str) -> str:
    """Conserta malformações específicas do LLM (preserva texto matemático tipo 'a < b > c'):
      <//tag>  →  </tag>     (double-slash close)
      <<tag>   →  <tag>      (double-opener, só se colado sem espaço)
      <tag/>   →  <tag>      (XHTML self-close de tag que normalmente tem conteúdo)
    Cada padrão exige a tag estar SEM espaço — pra não tocar em '<' avulso do texto."""
    allowed = "|".join(_ALLOWED_TG_TAGS)
    # <//tag> ou <///tag> → </tag>  (sem espaço entre < e /, e entre / e nome)
    text = re.sub(rf"<//+({allowed})>", r"</\1>", text, flags=re.IGNORECASE)
    # <<tag> ou <<<tag> → <tag>  (apenas múltiplos `<` consecutivos, sem espaço)
    text = re.sub(rf"<{{2,}}({allowed})(\s+[^<>]*?)?(/?)>", r"<\1\2\3>", text, flags=re.IGNORECASE)
    # <tag/> → <tag>  (XHTML self-close, sem espaço entre nome e /)
    text = re.sub(rf"<({allowed})/>", r"<\1>", text, flags=re.IGNORECASE)
    return text


def _balance_html_tags(text: str) -> str:
    """Sanitizador agressivo pra HTML do Telegram.

    Reconhece SÓ tags limpas e válidas (<b>, </b>, <code>, <code/>, <code attr="x">).
    Tags malformadas (<code/>/lembrete<//code>, <<, <div>, <b\n) e seus '<' '>'
    sobrevivem como TEXTO ESCAPADO — Telegram não rejeita a mensagem inteira.
    Balanceia open/close: open órfão fecha no fim; close órfão vira nada.
    """
    tokens: list[tuple[str, str]] = []
    i = 0
    for m in _CLEAN_TAG_RE.finditer(text):
        if m.start() > i:
            tokens.append(("text", text[i:m.start()]))
        slash_open = m.group(1)
        name = m.group(2).lower()
        slash_close = m.group(4)
        if slash_open == "/":
            tokens.append(("close", name))
        else:
            tokens.append(("open", name))
            if slash_close == "/":  # XHTML self-close vira open+close imediato
                tokens.append(("close", name))
        i = m.end()
    if i < len(text):
        tokens.append(("text", text[i:]))

    stack: list[str] = []
    out: list[str] = []
    for kind, val in tokens:
        if kind == "text":
            # Escapa < > & órfãos pra não confundir o parser do Telegram
            out.append(val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        elif kind == "open":
            stack.append(val)
            out.append(f"<{val}>")
        elif kind == "close":
            if val in stack:
                while stack and stack[-1] != val:
                    out.append(f"</{stack.pop()}>")
                stack.pop()
                out.append(f"</{val}>")
            # close órfão: descarta silenciosamente
    while stack:
        out.append(f"</{stack.pop()}>")
    return "".join(out)


def _guard(update: Update) -> bool:
    if ALLOWED_USER_ID is None:
        return True
    u = update.effective_user
    return u is not None and u.id == ALLOWED_USER_ID


def _current_model(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.bot_data.get("model", os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"))


def _source_emoji(source: str) -> str:
    return {"TACO": "✅", "VITAT": "🌿", "estimativa": "🟡", "sem dados": "❌"}.get(source, "•")


def _format_meal_message(meal_id: int, items: list[dict], totals: dict, model: str, notes: str | None) -> str:
    lines = []
    for it in items:
        emoji = _source_emoji(it["source"])
        matched = it.get("food_name") or "(sem match)"
        lines.append(
            f"{emoji} <b>{_esc(it['name_llm'])}</b> ({it['portion_g']:.0f}g) — "
            f"{it['kcal']:.0f} kcal\n"
            f"   <i>→ {_esc(matched)}</i>"
        )
    body = "\n".join(lines)
    notes_line = f"\n\n<i>{_esc(notes)}</i>" if notes else ""
    return (
        f"<b>#{meal_id}</b>\n{body}\n\n"
        f"🔥 <b>{totals['kcal']:.0f} kcal</b>  ·  "
        f"P {totals['protein_g']:.0f}  C {totals['carbs_g']:.0f}  G {totals['fat_g']:.0f}\n"
        f"<code>{_esc(model.split('/')[-1])}</code>"
        f"{notes_line}"
    )


def _meal_keyboard(meal_id: int, items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, it in enumerate(items[:8]):
        if it.get("alternatives"):
            rows.append([InlineKeyboardButton(
                f"✏️ trocar #{i+1} {it['name_llm'][:25]}",
                callback_data=f"fix:{meal_id}:{i}",
            )])
    rows.append([InlineKeyboardButton("🗑 apagar refeição", callback_data=f"del:{meal_id}")])
    return InlineKeyboardMarkup(rows)


# ============================================================
# Comandos — lógica em commands.py (agnóstica de canal)
# ============================================================

async def _send_result(update: Update, r: commands.CommandResult,
                       reply_markup=None) -> None:
    """Envia CommandResult: texto HTML (se houver) + foto (se houver)."""
    if r.text:
        try:
            await update.message.reply_text(
                r.text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            )
        except Exception:
            await update.message.reply_text(r.text, reply_markup=reply_markup)
    if r.png:
        await update.message.reply_photo(photo=io.BytesIO(r.png))


async def cmd_start(update: Update, _) -> None:
    u = update.effective_user
    log.info("start from user_id=%s username=%s", u.id, u.username)
    r = await commands.cmd_start(u.id, [])
    await _send_result(update, r, reply_markup=MAIN_KB)


async def cmd_ajuda(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_ajuda(update.effective_user.id, [])
    await _send_result(update, r, reply_markup=MAIN_KB)


async def cmd_modelo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram-only: aceita escrita (atualiza bot_data em memória)."""
    if not _guard(update): return
    if context.args:
        new = " ".join(context.args).strip()
        context.bot_data["model"] = new
        await update.message.reply_text(f"Modelo de visão agora: {new}")
    else:
        await update.message.reply_text(
            f"Visão: {_current_model(context)}\n"
            f"Chat (agente): {os.environ.get('OPENROUTER_CHAT_MODEL', 'google/gemma-4-31b-it')}"
        )


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    r = await commands.cmd_buscar(update.effective_user.id, list(context.args or []))
    await _send_result(update, r)


async def cmd_hoje(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_hoje(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_semana(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_semana(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_apagar(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_apagar(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_grafico(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_grafico(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    r = await commands.cmd_lembrete(update.effective_user.id, list(context.args or []))
    await _send_result(update, r)


async def cmd_reset(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_reset(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_perfil(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_perfil(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    r = await commands.cmd_relatorio(update.effective_user.id, list(context.args or []))
    await _send_result(update, r)


# ============================================================
# Fotos e mensagens
# ============================================================

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TODA foto passa pelo agente — ele decide se é prato, cardápio ou relógio."""
    if not _guard(update): return
    msg = update.message
    caption = (msg.caption or "").strip()
    photo = msg.photo[-1]
    await _agent_handle(update, context, text=caption, photo_file_id=photo.file_id)


# ============================================================
# Reply keyboard: dispatcher de botões
# ============================================================
async def _btn_refeicao(update: Update, _) -> None:
    await update.message.reply_text(
        "🍽 <b>Logar refeição</b>\n\n"
        "Você pode:\n"
        "• Mandar foto do prato → eu analiso\n"
        "• Descrever em texto: 'comi 100g arroz e 150g frango'\n"
        "• Mandar foto de cardápio + 'quero algo com carne'",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )


async def _btn_peso(update: Update, _) -> None:
    await update.message.reply_text(
        "⚖️ <b>Logar peso</b>\n\n"
        "Manda só o número (ex: <code>101.8</code>) ou foto da balança.\n"
        "Eu atualizo seu peso e recalculo a meta automaticamente.",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )


async def _btn_treino(update: Update, _) -> None:
    await update.message.reply_text(
        "🏃 <b>Logar treino</b>\n\n"
        "Você pode:\n"
        "• Mandar foto do relógio (Apple Watch, Garmin, Strava)\n"
        "• Descrever em texto: 'fiz 1h de corrida, 480 kcal'",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KB,
    )


async def _btn_mais(update: Update, _) -> None:
    await update.message.reply_text("⚙️ Mais opções:", reply_markup=MORE_KB)


async def _btn_voltar(update: Update, _) -> None:
    await update.message.reply_text("⬅️ Voltar", reply_markup=MAIN_KB)


async def _btn_buscar(update: Update, _) -> None:
    await update.message.reply_text(
        "🔍 Use: <code>/buscar &lt;termo&gt;</code>\nEx: <code>/buscar arroz integral</code>",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
    )


async def _btn_relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Sem args → semana
    context.args = []
    await cmd_relatorio(update, context)


# Mapa: texto do botão → handler
BUTTON_HANDLERS = {
    "🍽 Refeição": _btn_refeicao,
    "⚖️ Peso": _btn_peso,
    "🏃 Treino": _btn_treino,
    "📊 Hoje": lambda u, c: cmd_hoje(u, c),
    "🎯 Meta": lambda u, c: cmd_perfil(u, c),
    "⚙️ Mais": _btn_mais,
    "📈 Relatório": _btn_relatorio,
    "⏰ Lembrete": lambda u, c: cmd_lembrete(u, c),
    "🔍 Buscar": _btn_buscar,
    "❓ Ajuda": lambda u, c: cmd_ajuda(u, c),
    "🔄 Nova conversa": lambda u, c: cmd_reset(u, c),
    "⬅️ Voltar": _btn_voltar,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    text = update.message.text or ""
    # Intercepta botões antes do agente (não gasta tokens)
    handler = BUTTON_HANDLERS.get(text.strip())
    if handler:
        await handler(update, context)
        return
    await _agent_handle(update, context, text=text, photo_file_id=None)


async def _telegram_download_photo(bot, file_id: str) -> bytes | None:
    """Adapter: baixa foto pelo file_id do Telegram, retorna bytes."""
    f = await bot.get_file(file_id)
    buf = io.BytesIO()
    await f.download_to_memory(out=buf)
    return buf.getvalue()


async def _agent_handle(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        text: str, photo_file_id: str | None) -> None:
    """Rota pro agente conversacional."""
    msg = update.message
    await msg.chat.send_action(ChatAction.TYPING)

    async def _dl(pid: str) -> bytes | None:
        return await _telegram_download_photo(context.bot, pid)

    try:
        reply = await agent.run_turn(
            user_id=update.effective_user.id,
            user_text=text,
            photo_file_id=photo_file_id,
            download_photo=_dl,
            vision_model=_current_model(context),
        )
    except Exception as e:
        log.exception("agent.run_turn failed")
        await msg.reply_text(f"Agente quebrou: {e}")
        return

    # Converte markdown comum pra HTML (Gemma às vezes escapa do prompt)
    reply = _md_to_html(reply)
    try:
        await msg.reply_text(reply, parse_mode=ParseMode.HTML)
    except Exception:
        await msg.reply_text(reply)

    # Se o agente gerou um gráfico (via generate_report_chart), envia agora
    conv = await agent.get_or_create_conversation(update.effective_user.id)
    chart_bytes = await db.pop_pending_chart(conv["id"])
    if chart_bytes:
        await msg.reply_photo(photo=io.BytesIO(chart_bytes))


# ============================================================
# Callbacks (botões inline)
# ============================================================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user_id = update.effective_user.id

    if data.startswith("del:"):
        meal_id = int(data.split(":")[1])
        ok = await db.delete_meal(user_id, meal_id)
        await q.edit_message_text(f"Removido #{meal_id}." if ok else "Não consegui apagar.")
        return

    if data.startswith("fix:"):
        _, meal_id_s, idx_s = data.split(":")
        meal_id, idx = int(meal_id_s), int(idx_s)
        meal = await db.get_meal(meal_id, user_id)
        if not meal:
            await q.edit_message_text("Refeição não encontrada.")
            return
        items = meal["items"] if isinstance(meal["items"], list) else json.loads(meal["items"])
        item = items[idx]
        alts = item.get("alternatives") or []
        if not alts:
            await q.message.reply_text("Sem alternativas pra esse item.")
            return
        rows = []
        for a in alts[:5]:
            rows.append([InlineKeyboardButton(
                f"{a['name'][:50]} ({a['score']:.2f})",
                callback_data=f"pick:{meal_id}:{idx}:{a['id']}",
            )])
        rows.append([InlineKeyboardButton("cancelar", callback_data="noop")])
        await q.message.reply_text(
            f"Trocar match de '<b>{_esc(item['name_llm'])}</b>' por:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data.startswith("pick:"):
        _, meal_id_s, idx_s, food_id_s = data.split(":")
        meal_id, idx, food_id = int(meal_id_s), int(idx_s), int(food_id_s)
        meal = await db.get_meal(meal_id, user_id)
        if not meal:
            await q.edit_message_text("Refeição não encontrada.")
            return
        items = meal["items"] if isinstance(meal["items"], list) else json.loads(meal["items"])
        name_llm = items[idx]["name_llm"]
        new_items, totals = await calculator.recalc_with_override(items, idx, food_id)
        await db.update_meal_items(meal_id, new_items, totals)
        await matcher.save_alias(name_llm, food_id, user_id=user_id)
        model = meal["vision_model"]
        await q.edit_message_text(
            _format_meal_message(meal_id, new_items, totals, model, meal.get("notes")),
            parse_mode=ParseMode.HTML,
            reply_markup=_meal_keyboard(meal_id, new_items),
        )
        return

    if data == "noop":
        await q.message.delete()
        return


# ============================================================
# Lifecycle
# ============================================================

async def _weigh_in_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que roda a cada minuto. Dispara lembretes pra users no horário local certo."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    try:
        users = await db.users_due_for_weigh_in_reminder()
    except Exception:
        log.exception("falha buscando users pra lembrete")
        return

    now_utc = _dt.now(timezone.utc)
    for u in users:
        # timezone canônico do user (fallback weigh_in_tz legado)
        tz_name = u.get("timezone") or u.get("weigh_in_tz") or "America/Sao_Paulo"
        try:
            local = now_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            continue
        hour = u.get("weigh_in_hour", 6)
        minute = u.get("weigh_in_minute") or 0
        # match exato de HH:MM no horário local do user
        if local.hour != hour or local.minute != minute:
            continue
        today_local = local.date()
        if u.get("weigh_in_last_date") == today_local:
            continue  # já enviado hoje

        reminder_text = (
            "☀️ Bom dia! Hora da pesagem.\n\n"
            "Manda só o número (ex: 101.8) ou foto da balança "
            "que eu atualizo seu peso e recalculo a meta do dia."
        )
        sent_any = False
        try:
            await context.bot.send_message(
                chat_id=u["user_id"],
                text=reminder_text,
            )
            sent_any = True
            log.info("lembrete telegram enviado pra user_id=%s (%02d:%02d local)",
                     u["user_id"], hour, minute)
        except Exception:
            log.exception("falha enviando lembrete telegram user_id=%s", u.get("user_id"))

        # WhatsApp: se este user_id está vinculado a um (ou mais) números, manda lá também.
        # (single-user: WHATSAPP_LINK_PHONE comma-sep ↔ ALLOWED_USER_ID)
        wa_phones_raw = os.environ.get("WHATSAPP_LINK_PHONE", "")
        allowed = os.environ.get("ALLOWED_USER_ID")
        wa_phones = [p.strip() for p in wa_phones_raw.split(",") if p.strip()]
        if (wa_phones and allowed and str(u["user_id"]) == allowed
                and os.environ.get("TWILIO_ACCOUNT_SID")):
            from whatsapp import send_whatsapp_message
            for phone in wa_phones:
                try:
                    await send_whatsapp_message(phone, body=reminder_text)
                    sent_any = True
                    log.info("lembrete whatsapp enviado pra %s", phone)
                except Exception:
                    log.exception("falha enviando lembrete whatsapp pra %s", phone)

        if not sent_any:
            continue
        try:
            await db.mark_reminder_sent(u["user_id"], today_local)
            # Salva o lembrete no histórico da conversa do agente — assim quando o
            # user responder com foto/número, o agente tem o contexto pra interpretar.
            try:
                conv = await agent.get_or_create_conversation(u["user_id"])
                await agent.add_message(conv["id"], "assistant", content=reminder_text)
            except Exception:
                log.exception("falha salvando lembrete no histórico (não-crítico)")
        except Exception:
            log.exception("falha marcando lembrete enviado user_id=%s", u.get("user_id"))


async def _post_init(app: Application) -> None:
    await db.init()
    n = await db.count_foods()
    if n == 0:
        log.warning("Banco vazio! Rode: python import_taco.py")
    else:
        log.info("DB pronto. %s alimentos carregados.", n)
    # Job de lembrete: roda no minuto 0 de cada hora
    from datetime import time as _time
    app.job_queue.run_repeating(
        _weigh_in_job, interval=60, first=10, name="weigh_in_reminder"
    )
    log.info("Lembrete de pesagem agendado (verifica a cada minuto).")


async def _post_shutdown(app: Application) -> None:
    await db.close()


def _build_telegram_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["ajuda", "help"], cmd_ajuda))
    app.add_handler(CommandHandler("hoje", cmd_hoje))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("apagar", cmd_apagar))
    app.add_handler(CommandHandler("modelo", cmd_modelo))
    app.add_handler(CommandHandler("buscar", cmd_buscar))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("perfil", cmd_perfil))
    app.add_handler(CommandHandler("relatorio", cmd_relatorio))
    app.add_handler(CommandHandler("grafico", cmd_grafico))
    app.add_handler(CommandHandler("lembrete", cmd_lembrete))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def _whatsapp_enabled() -> bool:
    """WhatsApp adapter sobe só se as creds Twilio estiverem configuradas."""
    return all(
        os.environ.get(k)
        for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM",
                  "WHATSAPP_LINK_PHONE", "PUBLIC_BASE_URL")
    )


async def _run_all() -> None:
    """Roda Telegram polling + (opcional) FastAPI/Twilio webhook em paralelo."""
    import asyncio
    tg_app = _build_telegram_app()

    # Start Telegram (não-bloqueante)
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Telegram polling iniciado.")

    server = None
    if _whatsapp_enabled():
        import uvicorn
        from whatsapp import app as wa_app
        # Injeta o tg_app pro lembrete saber mandar via Telegram também
        wa_app.state.tg_bot = tg_app.bot
        port = int(os.environ.get("WHATSAPP_WEBHOOK_PORT", "8000"))
        host = os.environ.get("WHATSAPP_WEBHOOK_HOST", "0.0.0.0")
        config = uvicorn.Config(wa_app, host=host, port=port, log_level="info",
                                lifespan="on")
        server = uvicorn.Server(config)
        log.info("WhatsApp adapter iniciando em %s:%s", host, port)
        server_task = asyncio.create_task(server.serve())
    else:
        log.info("WhatsApp adapter desabilitado (faltam creds Twilio no .env). "
                 "Rodando só Telegram.")
        server_task = None

    try:
        # Bloqueia até alguém quebrar (Ctrl+C → KeyboardInterrupt no asyncio.run)
        if server_task:
            await server_task
        else:
            # Sem WhatsApp: dorme até cancelar
            while True:
                await asyncio.sleep(3600)
    finally:
        log.info("Encerrando...")
        if server is not None:
            server.should_exit = True
        try:
            await tg_app.updater.stop()
        except Exception:
            pass
        await tg_app.stop()
        await tg_app.shutdown()


def main() -> None:
    import asyncio
    log.info("Bot rodando. Visão: %s | Chat: %s",
             os.environ.get("OPENROUTER_MODEL"),
             os.environ.get("OPENROUTER_CHAT_MODEL", "google/gemma-4-31b-it"))
    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        log.info("interrompido por usuário")


if __name__ == "__main__":
    main()
