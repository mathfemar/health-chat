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
        [KeyboardButton("🔁 Repetir"), KeyboardButton("📈 Relatório"), KeyboardButton("⏰ Lembrete")],
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

def _suggestions_to_inline_kb(suggestions: list[commands.Suggestion]) -> InlineKeyboardMarkup:
    """Converte Suggestion[] em InlineKeyboardMarkup (botões clicáveis no Telegram).
    Cada botão dispara on_callback com data 'cmd:<command>'."""
    rows = [[InlineKeyboardButton(s.label, callback_data=f"cmd:{s.command}")]
            for s in suggestions]
    return InlineKeyboardMarkup(rows)


async def _send_result(update: Update, r: commands.CommandResult,
                       reply_markup=None) -> None:
    """Envia CommandResult: texto HTML + foto + sugestões (inline keyboard)."""
    # Se há sugestões e o caller não definiu reply_markup, usa as sugestões.
    if r.suggestions and reply_markup is None:
        reply_markup = _suggestions_to_inline_kb(r.suggestions)
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
            f"Chat (agente): {os.environ.get('OPENROUTER_CHAT_MODEL', 'deepseek/deepseek-chat-v3.1:free')}"
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


async def cmd_salvar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Salva a última refeição logada como template.
    Uso: /salvar <nome>      ex: /salvar whey com leite
    """
    if not _guard(update): return
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Uso: <code>/salvar &lt;nome&gt;</code>\n"
            "Ex: <code>/salvar whey com leite</code>\n\n"
            "Salva sua ÚLTIMA refeição logada como template. "
            "Depois você usa <code>/r whey</code> pra repetir.",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
        )
        return
    name = " ".join(context.args).strip()
    last = await db.get_last_meal(user_id)
    if not last:
        await update.message.reply_text("Sem refeição recente pra salvar. Loga uma primeiro.")
        return
    totals = {
        "kcal": float(last["kcal"]), "protein_g": float(last["protein_g"]),
        "carbs_g": float(last["carbs_g"]), "fat_g": float(last["fat_g"]),
    }
    try:
        tid = await db.insert_meal_template(user_id, name, last["items"], totals)
    except ValueError as e:
        await update.message.reply_text(f"Erro: {e}")
        return
    await update.message.reply_text(
        f"✅ Template <b>{_esc(name)}</b> salvo (#{tid})\n"
        f"🔥 {totals['kcal']:.0f} kcal · P {totals['protein_g']:.0f}g\n\n"
        f"Use <code>/r {name.split()[0]}</code> pra logar de novo.",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
    )


async def cmd_repetir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Loga template direto. Uso: /r <nome>  ex: /r whey
    Sem args → lista templates disponíveis."""
    if not _guard(update): return
    user_id = update.effective_user.id
    if not context.args:
        # Lista templates como botões clicáveis
        templates = await db.list_meal_templates(user_id, limit=10)
        if not templates:
            await update.message.reply_text(
                "Você não tem refeições salvas ainda.\n\n"
                "Loga uma refeição, depois use <code>/salvar nome</code> pra criar um template.",
                parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
            )
            return
        rows = []
        for t in templates:
            tot = t["totals"]
            label = f"{t['name']} — {tot.get('kcal', 0):.0f} kcal"
            rows.append([InlineKeyboardButton(label[:60],
                                              callback_data=f"tpl:{t['id']}")])
        await update.message.reply_text(
            "Qual refeição salva você quer logar?",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    name = " ".join(context.args).strip()
    t = await db.find_meal_template(user_id, name)
    if not t:
        await update.message.reply_text(
            f"Não achei template '<b>{_esc(name)}</b>'. Use <code>/r</code> sem args pra ver a lista.",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
        )
        return
    await _log_template_and_reply(update, user_id, t)


async def _log_template_and_reply(update: Update, user_id: int, template: dict) -> None:
    meal_id = await db.insert_meal(
        user_id=user_id, vision_model="template", photo_file_id=None,
        items=template["items"], totals=template["totals"],
        notes=f"template:{template['name']}",
    )
    await db.mark_template_used(template["id"])
    tot = template["totals"]
    await update.message.reply_text(
        f"✅ <b>{_esc(template['name'])}</b> logado (#{meal_id})\n"
        f"🔥 {tot.get('kcal', 0):.0f} kcal · "
        f"P {tot.get('protein_g', 0):.0f}  C {tot.get('carbs_g', 0):.0f}  G {tot.get('fat_g', 0):.0f}",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
    )
    # Anexa gráfico do dia
    try:
        from agent.tools import _build_daily_chart
        png = await _build_daily_chart(user_id)
        await update.message.reply_photo(photo=io.BytesIO(png))
    except Exception:
        log.exception("falha no gráfico após /r")


async def cmd_grafico(update: Update, _) -> None:
    if not _guard(update): return
    r = await commands.cmd_grafico(update.effective_user.id, [])
    await _send_result(update, r)


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Configura push proativo (almoço, jantar, sextou).
    Uso:
      /push          → mostra config atual
      /push off      → desliga todos os pushes proativos
      /push on       → liga (mantém horários)
      /push almoco 13:30  → muda horário do almoço
      /push jantar 20:30  → muda horário do jantar
    """
    if not _guard(update): return
    user_id = update.effective_user.id
    p = await db.get_profile(user_id) or {}
    args = context.args

    if not args:
        enabled = p.get("push_enabled", True)
        lh = p.get("push_lunch_hour", 13); lm = p.get("push_lunch_minute", 0) or 0
        dh = p.get("push_dinner_hour", 20); dm = p.get("push_dinner_minute", 0) or 0
        status = "✅ ligados" if enabled else "❌ desligados"
        await update.message.reply_text(
            f"Pushes proativos: {status}\n"
            f"  🍽 Almoço: <b>{lh:02d}:{lm:02d}</b>\n"
            f"  🌙 Jantar: <b>{dh:02d}:{dm:02d}</b>\n"
            f"  📊 Sextou: sexta 19:00\n\n"
            "Uso:\n"
            "  <code>/push off</code> — desliga\n"
            "  <code>/push on</code>  — liga\n"
            "  <code>/push almoco 13:30</code>\n"
            "  <code>/push jantar 20:30</code>",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
        )
        return

    sub = args[0].lower()
    if sub == "off":
        await db.upsert_profile_field(user_id, "push_enabled", False)
        await update.message.reply_text("Pushes desligados.")
        return
    if sub == "on":
        await db.upsert_profile_field(user_id, "push_enabled", True)
        await update.message.reply_text("Pushes ligados.")
        return
    if sub in ("almoco", "almoço", "jantar") and len(args) >= 2:
        parsed = _parse_hhmm(args[1])
        if not parsed:
            await update.message.reply_text("Formato inválido. Ex: /push almoco 13:30")
            return
        h, m = parsed
        if sub == "jantar":
            await db.upsert_profile_field(user_id, "push_dinner_hour", h)
            await db.upsert_profile_field(user_id, "push_dinner_minute", m)
            label = "Jantar"
        else:
            await db.upsert_profile_field(user_id, "push_lunch_hour", h)
            await db.upsert_profile_field(user_id, "push_lunch_minute", m)
            label = "Almoço"
        await db.upsert_profile_field(user_id, "push_enabled", True)
        await update.message.reply_text(f"{label}: {h:02d}:{m:02d} ✅")
        return

    await update.message.reply_text(
        "Uso: /push [off|on|almoco HH:MM|jantar HH:MM]"
    )


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


async def _btn_repetir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Atalho pro /r sem args — lista templates como botões."""
    context.args = []
    await cmd_repetir(update, context)


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
    "🔁 Repetir": _btn_repetir,
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

    # tpl:<template_id> — clique num template da lista do /r
    if data.startswith("tpl:"):
        try:
            tpl_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.message.reply_text("ID de template inválido.")
            return
        t = await db.find_meal_template(user_id, tpl_id)
        if not t:
            await q.message.reply_text("Template não encontrado.")
            return
        meal_id = await db.insert_meal(
            user_id=user_id, vision_model="template", photo_file_id=None,
            items=t["items"], totals=t["totals"],
            notes=f"template:{t['name']}",
        )
        await db.mark_template_used(t["id"])
        tot = t["totals"]
        await q.edit_message_text(
            f"✅ <b>{_esc(t['name'])}</b> logado (#{meal_id})\n"
            f"🔥 {tot.get('kcal', 0):.0f} kcal · "
            f"P {tot.get('protein_g', 0):.0f}  C {tot.get('carbs_g', 0):.0f}  G {tot.get('fat_g', 0):.0f}",
            parse_mode=ParseMode.HTML,
        )
        # Anexa gráfico
        try:
            from agent.tools import _build_daily_chart
            png = await _build_daily_chart(user_id)
            await q.message.reply_photo(photo=io.BytesIO(png))
        except Exception:
            log.exception("falha no gráfico após tpl:")
        return

    # cmd:<slash_command> — dispatch genérico de Suggestion (botão de sugestão)
    if data.startswith("cmd:"):
        slash = data[len("cmd:"):].strip()
        parsed = commands.parse_slash(slash if slash.startswith("/") else f"/{slash}")
        if not parsed:
            await q.message.reply_text(f"Comando inválido: {slash}")
            return
        cmd_name, args = parsed
        handler = commands.COMMAND_HANDLERS.get(cmd_name)
        if not handler:
            await q.message.reply_text(f"Comando /{cmd_name} não reconhecido.")
            return
        try:
            r = await handler(user_id, args)
        except Exception as e:
            log.exception("erro executando suggestion /%s", cmd_name)
            await q.message.reply_text(f"Erro em /{cmd_name}: {e}")
            return
        # Re-usa _send_result-like inline (sem o update.message direto)
        markup = _suggestions_to_inline_kb(r.suggestions) if r.suggestions else None
        if r.text:
            try:
                await q.message.reply_text(r.text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except Exception:
                await q.message.reply_text(r.text, reply_markup=markup)
        if r.png:
            await q.message.reply_photo(photo=io.BytesIO(r.png))
        return

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

async def _should_skip_nudge_due_to_recent_meal(user_id: int, since_minutes: int) -> bool:
    """True se houve refeição logada nos últimos N minutos — não precisa cutucar."""
    from datetime import datetime as _dt
    last = await db.last_meal_at(user_id)
    if not last:
        return False
    delta = _dt.now(timezone.utc) - last
    return delta.total_seconds() < since_minutes * 60


async def _push_nudge_generic(context, kind: str, hour_col_h: str, hour_col_m: str,
                              last_date_col: str, skip_window_min: int,
                              message_fn) -> None:
    """Helper genérico pra disparo de lunch/dinner nudge."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from agent import runtime as agent_rt

    try:
        users = await db.users_due_for_push()
    except Exception:
        log.exception("falha buscando users pra push %s", kind)
        return

    now_utc = _dt.now(timezone.utc)
    for u in users:
        tz_name = u.get("timezone") or "America/Sao_Paulo"
        try:
            local = now_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            continue
        hour = u.get(hour_col_h)
        minute = u.get(hour_col_m) or 0
        if hour is None or local.hour != hour or local.minute != minute:
            continue
        today_local = local.date()
        if u.get(last_date_col) == today_local:
            continue
        # Skip se logou refeição recente
        if await _should_skip_nudge_due_to_recent_meal(u["user_id"], skip_window_min):
            await db.mark_push_sent(u["user_id"], kind, today_local)  # marca pra não ficar checando
            continue
        try:
            text = message_fn()
            await context.bot.send_message(chat_id=u["user_id"], text=text)
            await db.mark_push_sent(u["user_id"], kind, today_local)
            # Salva no histórico da conversa do agente — assim se user responder com
            # foto/texto, o agente tem o contexto certo
            try:
                conv = await agent_rt.get_or_create_conversation(u["user_id"])
                await agent_rt.add_message(conv["id"], "assistant", content=text)
            except Exception:
                log.exception("falha salvando nudge no histórico")
            log.info("push %s enviado pra user_id=%s", kind, u["user_id"])
        except Exception:
            log.exception("falha enviando push %s pra user_id=%s", kind, u.get("user_id"))


async def _lunch_nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    from agent.nudges import pick_lunch
    await _push_nudge_generic(
        context, kind="lunch",
        hour_col_h="push_lunch_hour", hour_col_m="push_lunch_minute",
        last_date_col="push_lunch_last_date",
        skip_window_min=120,  # 2h
        message_fn=pick_lunch,
    )


async def _dinner_nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    from agent.nudges import pick_dinner
    await _push_nudge_generic(
        context, kind="dinner",
        hour_col_h="push_dinner_hour", hour_col_m="push_dinner_minute",
        last_date_col="push_dinner_last_date",
        skip_window_min=180,  # 3h
        message_fn=pick_dinner,
    )


async def _friday_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sexta às 19h local, manda resumo + gráfico da semana."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from agent.nudges import pick_friday
    from agent.tools import _build_daily_chart  # noqa — usa pra reaproveitar
    from agent import charts
    import io as _io

    try:
        users = await db.users_due_for_push()
    except Exception:
        log.exception("falha buscando users pra friday")
        return

    now_utc = _dt.now(timezone.utc)
    for u in users:
        tz_name = u.get("timezone") or "America/Sao_Paulo"
        try:
            local = now_utc.astimezone(ZoneInfo(tz_name))
        except Exception:
            continue
        # Sexta = weekday 4; 19:00 local
        if local.weekday() != 4 or local.hour != 19 or local.minute != 0:
            continue
        today_local = local.date()
        if u.get("push_friday_last_date") == today_local:
            continue
        try:
            intro = pick_friday()
            summary = await db.period_summary(u["user_id"], 7)
            profile = await db.get_profile(u["user_id"]) or {}
            goal = profile.get("daily_kcal")
            totals = summary["totals"]
            avgs = summary["averages"]
            text = (
                f"{intro}\n\n"
                f"📊 7 dias:\n"
                f"• Total: {totals['intake_kcal']:.0f} kcal consumidas, "
                f"{totals['burned_kcal']} queimadas\n"
                f"• Média: {avgs['intake_per_day']:.0f} kcal/dia"
                + (f" (meta {goal})" if goal else "") + "\n\n"
                "Bora pra próxima semana 💪"
            )
            await context.bot.send_message(chat_id=u["user_id"], text=text)
            # Gráfico
            png = charts.daily_intake_vs_goal(summary["per_day"], goal_kcal=goal,
                                              title="Sua semana")
            await context.bot.send_photo(chat_id=u["user_id"], photo=_io.BytesIO(png))
            await db.mark_push_sent(u["user_id"], "friday", today_local)
            log.info("sextou enviado pra user_id=%s", u["user_id"])
        except Exception:
            log.exception("falha enviando friday summary user_id=%s", u.get("user_id"))


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
        # 3 cenários:
        # - ALLOWED_USER_ID definido: só manda se user_id bater (single-user).
        # - ALLOWED_USER_ID vazio: tenta casar via phone-derived id (multi-user).
        # - WHATSAPP_LINK_PHONE vazio: pula (não sabemos pra qual phone mandar).
        wa_phones_raw = os.environ.get("WHATSAPP_LINK_PHONE", "")
        allowed = os.environ.get("ALLOWED_USER_ID")
        wa_phones = [p.strip() for p in wa_phones_raw.split(",") if p.strip()]
        if wa_phones and os.environ.get("TWILIO_ACCOUNT_SID"):
            from whatsapp import send_whatsapp_message, _phone_to_user_id
            # Decide se este user_id corresponde a algum dos telefones configurados
            if allowed:
                target_phones = wa_phones if str(u["user_id"]) == allowed else []
            else:
                target_phones = [p for p in wa_phones
                                 if _phone_to_user_id(p) == u["user_id"]]
            for phone in target_phones:
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
    # Jobs agendados — todos checam a cada minuto e disparam conforme hora local do user
    app.job_queue.run_repeating(_weigh_in_job, interval=60, first=10, name="weigh_in_reminder")
    app.job_queue.run_repeating(_lunch_nudge_job, interval=60, first=20, name="lunch_nudge")
    app.job_queue.run_repeating(_dinner_nudge_job, interval=60, first=30, name="dinner_nudge")
    app.job_queue.run_repeating(_friday_summary_job, interval=60, first=40, name="friday_summary")
    log.info("Lembrete de pesagem + 3 nudges agendados (todos checam a cada minuto).")


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
    app.add_handler(CommandHandler("push", cmd_push))
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
             os.environ.get("OPENROUTER_CHAT_MODEL", "deepseek/deepseek-chat-v3.1:free"))
    try:
        asyncio.run(_run_all())
    except KeyboardInterrupt:
        log.info("interrompido por usuário")


if __name__ == "__main__":
    main()
