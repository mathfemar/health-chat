import html
import io
import json
import logging
import os
import re
from datetime import timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
import db
import llm
import matcher
from agent import runtime as agent

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("health-chat")

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"]) if os.getenv("ALLOWED_USER_ID") else None


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
# Comandos
# ============================================================

COMMANDS_HELP = """\
📷 <b>Foto</b> (prato, cardápio, relógio fitness ou balança) → agente decide
💬 <b>Texto livre</b> → conversa natural com o agente

Ex: 'quero definir minha meta', 'foto do cardápio, quero algo com carne',
    'como tá meu dia?', 'fiz 1h de corrida', '101.8' (loga peso direto)

<b>Comandos diretos:</b>
/start — esta mensagem
/ajuda — mesma coisa
/perfil — vê seu perfil e meta calórica
/hoje — refeições do dia + total
/semana — total dos últimos 7 dias
/grafico — gráfico do dia (anel kcal + macros + refeições)
/relatorio [semana|mes|N] — gráfico de intake vs queimado vs meta
/lembrete [off|on|HH:MM] — lembrete diário de pesagem (ex: 6:30, 7, 06:35)
/apagar — remove a última refeição
/buscar &lt;termo&gt; — busca alimento no banco
/modelo [slug] — vê/troca o modelo de visão
/reset — começa nova conversa com o agente

<b>Marcadores nas respostas:</b>
✅ TACO   🌿 Vitat   🟡 estimativa   ❌ sem dados
"""


async def cmd_start(update: Update, _) -> None:
    u = update.effective_user
    log.info("start from user_id=%s username=%s", u.id, u.username)
    n = await db.count_foods()
    await update.message.reply_text(
        f"Oi! Seu user_id é <code>{u.id}</code>. Base nutricional: {n} alimentos.\n\n"
        f"{COMMANDS_HELP}\n"
        "👉 Sem perfil ainda? Diga <i>'quero definir minha meta'</i> pra começar o onboarding.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_ajuda(update: Update, _) -> None:
    if not _guard(update): return
    await update.message.reply_text(COMMANDS_HELP, parse_mode=ParseMode.HTML)


async def cmd_modelo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    if not context.args:
        await update.message.reply_text("Uso: /buscar arroz integral")
        return
    name = " ".join(context.args)
    m = await matcher.match_one(name)
    if not m.alternatives:
        await update.message.reply_text("Nada encontrado.")
        return
    lines = [f"Para '{_esc(name)}':"]
    for a in m.alternatives[:5]:
        marker = " ⭐" if a["id"] == m.food_id else ""
        lines.append(f"  • {_esc(a['name'])} (sim {a['score']:.2f}){marker}")
    lines.append(f"\nMétodo: {m.method}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_hoje(update: Update, _) -> None:
    if not _guard(update): return
    from zoneinfo import ZoneInfo
    user_id = update.effective_user.id
    profile = await db.get_profile(user_id) or {}
    tz_name = profile.get("timezone") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")
    meals = await db.list_today(user_id)
    if not meals:
        await update.message.reply_text("Nada registrado hoje.")
        return
    lines = []
    tot_k = tot_p = tot_c = tot_f = 0.0
    for m in meals:
        items = m["items"] if isinstance(m["items"], list) else json.loads(m["items"])
        names = ", ".join(i.get("food_name") or i["name_llm"] for i in items[:3])
        local_time = m["eaten_at"].astimezone(tz).strftime("%H:%M")
        lines.append(f"#{m['id']}  {local_time}  {_esc(names)}  — {float(m['kcal']):.0f} kcal")
        tot_k += float(m["kcal"]); tot_p += float(m["protein_g"])
        tot_c += float(m["carbs_g"]); tot_f += float(m["fat_g"])
    await update.message.reply_text(
        f"<b>Hoje</b> ({len(meals)} refeições)\n" + "\n".join(lines) +
        f"\n\n🔥 <b>{tot_k:.0f} kcal</b>\nP {tot_p:.0f}  C {tot_c:.0f}  G {tot_f:.0f}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_semana(update: Update, _) -> None:
    if not _guard(update): return
    from datetime import datetime, timedelta, timezone
    s = await db.summary_since(update.effective_user.id, datetime.now(timezone.utc) - timedelta(days=7))
    if not s["n"]:
        await update.message.reply_text("Sem refeições nos últimos 7 dias.")
        return
    await update.message.reply_text(
        f"<b>Últimos 7 dias</b> — {s['n']} refeições\n"
        f"Total: {float(s['kcal']):.0f} kcal\n"
        f"Média/dia: {float(s['kcal'])/7:.0f} kcal\n"
        f"P {float(s['protein_g']):.0f}  C {float(s['carbs_g']):.0f}  G {float(s['fat_g']):.0f}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_apagar(update: Update, _) -> None:
    if not _guard(update): return
    d = await db.delete_last(update.effective_user.id)
    await update.message.reply_text(f"Removido #{d}." if d else "Nada pra apagar.")


async def cmd_grafico(update: Update, _) -> None:
    """Gera e envia o gráfico do dia direto, sem passar pelo agente."""
    if not _guard(update): return
    from agent.tools import _build_daily_chart
    user_id = update.effective_user.id
    try:
        png = await _build_daily_chart(user_id)
    except Exception as e:
        log.exception("erro gerando gráfico")
        await update.message.reply_text(f"Falha gerando gráfico: {e}")
        return
    await update.message.reply_photo(photo=io.BytesIO(png))


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    """Aceita: '6', '06', '6:30', '06:30', '6.30', '630', '0630'.
    Retorna (hour, minute) ou None."""
    s = s.strip().replace(".", ":").replace("h", ":")
    if ":" in s:
        try:
            h_s, m_s = s.split(":", 1)
            h, m = int(h_s), int(m_s)
        except ValueError:
            return None
    elif s.isdigit():
        if len(s) <= 2:
            h, m = int(s), 0
        elif len(s) in (3, 4):
            h, m = int(s[:-2]), int(s[-2:])
        else:
            return None
    else:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


async def cmd_lembrete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Configurar lembrete diário de pesagem.
    Uso:
      /lembrete            → mostra atual
      /lembrete off        → desliga
      /lembrete on         → liga (mantém horário atual)
      /lembrete 7          → muda pra 07:00
      /lembrete 6:30       → muda pra 06:30
      /lembrete 06:35      → muda pra 06:35
    """
    if not _guard(update): return
    user_id = update.effective_user.id
    args = context.args
    p = await db.get_profile(user_id) or {}

    if not args:
        enabled = p.get("weigh_in_enabled", True)
        hour = p.get("weigh_in_hour", 6) or 6
        minute = p.get("weigh_in_minute", 0) or 0
        status = "✅ ligado" if enabled else "❌ desligado"
        await update.message.reply_text(
            f"Lembrete de pesagem: {status} às <b>{hour:02d}:{minute:02d}</b> "
            f"({p.get('weigh_in_tz','America/Sao_Paulo')})\n\n"
            "Uso:\n"
            "  <code>/lembrete off</code>  — desliga\n"
            "  <code>/lembrete on</code>   — liga\n"
            "  <code>/lembrete 7</code>    — 07:00\n"
            "  <code>/lembrete 6:30</code> — 06:30\n"
            "  <code>/lembrete 06:35</code> — 06:35",
            parse_mode=ParseMode.HTML,
        )
        return

    arg = args[0].lower()
    if arg == "off":
        await db.upsert_profile_field(user_id, "weigh_in_enabled", False)
        await update.message.reply_text("Lembrete desligado.")
        return
    if arg == "on":
        await db.upsert_profile_field(user_id, "weigh_in_enabled", True)
        h = p.get("weigh_in_hour", 6) or 6
        m = p.get("weigh_in_minute", 0) or 0
        await update.message.reply_text(f"Lembrete ligado às {h:02d}:{m:02d}.")
        return

    parsed = _parse_hhmm(arg)
    if parsed is None:
        await update.message.reply_text(
            "Formato inválido. Use:\n"
            "  /lembrete 7        (07:00)\n"
            "  /lembrete 6:30     (06:30)\n"
            "  /lembrete off"
        )
        return

    h, m = parsed
    await db.upsert_profile_field(user_id, "weigh_in_hour", h)
    await db.upsert_profile_field(user_id, "weigh_in_minute", m)
    await db.upsert_profile_field(user_id, "weigh_in_enabled", True)
    await update.message.reply_text(f"Lembrete configurado pra <b>{h:02d}:{m:02d}</b>.",
                                     parse_mode=ParseMode.HTML)


async def cmd_reset(update: Update, _) -> None:
    """Fecha a conversa atual com o agente. Próxima msg cria uma nova."""
    if not _guard(update): return
    await db.close_active_conversation(update.effective_user.id)
    await update.message.reply_text("Conversa zerada. 🔄 Próxima mensagem começa do zero.")


async def cmd_perfil(update: Update, _) -> None:
    """Mostra perfil + meta. Onboarding fica com o agente."""
    if not _guard(update): return
    p = await db.get_profile(update.effective_user.id)
    if not p:
        await update.message.reply_text(
            "Sem perfil ainda. Manda uma mensagem tipo 'quero definir minha meta' "
            "que eu te conduzo no onboarding."
        )
        return
    missing = [k for k in ("sex", "birth_date", "height_cm", "current_weight_kg",
                            "target_weight_kg", "activity_level", "weekly_rate_kg")
               if p.get(k) is None]
    lines = [
        f"<b>Perfil</b> de {_esc(p.get('name') or '—')}",
        f"  Sexo: {_esc(p.get('sex') or '—')}",
        f"  Nascimento: {_esc(p.get('birth_date') or '—')}",
        f"  Altura: {p.get('height_cm') or '—'} cm",
        f"  Peso atual: {p.get('current_weight_kg') or '—'} kg",
        f"  Peso-alvo: {p.get('target_weight_kg') or '—'} kg",
        f"  Atividade: {_esc(p.get('activity_level') or '—')}",
        f"  Ritmo: {p.get('weekly_rate_kg') or '—'} kg/semana",
        f"  Eat-back: {p.get('eatback_pct') or 100}%",
    ]
    if p.get("daily_kcal"):
        lines.append(f"\n🎯 <b>Meta: {p['daily_kcal']} kcal/dia</b> ({p.get('daily_protein_g')}g proteína)")
    if missing:
        lines.append(f"\n⚠️ Faltam: {', '.join(missing)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_relatorio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Relatório do período + gráfico. Args: 'semana' (7d) ou 'mes' (30d) ou número."""
    if not _guard(update): return
    arg = (context.args[0].lower() if context.args else "semana")
    days = 30 if arg in ("mes", "mês", "30") else 7
    if arg.isdigit():
        days = max(1, min(90, int(arg)))

    user_id = update.effective_user.id
    summary = await db.period_summary(user_id, days)
    p = await db.get_profile(user_id)
    goal = p.get("daily_kcal") if p else None

    # Texto resumo
    totals = summary["totals"]
    avgs = summary["averages"]
    text = (
        f"<b>Relatório — últimos {days} dias</b>\n"
        f"Intake total: {totals['intake_kcal']:.0f} kcal\n"
        f"Queimado total: {totals['burned_kcal']} kcal\n"
        f"Net total: {totals['net_kcal']:.0f} kcal\n\n"
        f"<b>Médias/dia:</b>\n"
        f"  Intake: {avgs['intake_per_day']:.0f} kcal"
        + (f" (meta {goal})" if goal else "") + "\n"
        f"  Queimado: {avgs['burned_per_day']:.0f} kcal\n"
        f"  Net: {avgs['net_per_day']:.0f} kcal"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # Gráfico
    from agent import charts
    png = charts.daily_intake_vs_goal(summary["per_day"], goal_kcal=goal,
                                       title=f"Últimos {days} dias")
    await update.message.reply_photo(photo=io.BytesIO(png))


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


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    await _agent_handle(update, context, text=update.message.text, photo_file_id=None)


async def _agent_handle(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        text: str, photo_file_id: str | None) -> None:
    """Rota pro agente conversacional."""
    msg = update.message
    await msg.chat.send_action(ChatAction.TYPING)
    try:
        reply = await agent.run_turn(
            user_id=update.effective_user.id,
            user_text=text,
            photo_file_id=photo_file_id,
            bot=context.bot,
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

        try:
            await context.bot.send_message(
                chat_id=u["user_id"],
                text=(
                    "☀️ Bom dia! Hora da pesagem.\n\n"
                    "Manda só o número (ex: <code>101.8</code>) ou foto da balança "
                    "que eu atualizo seu peso e recalculo a meta do dia."
                ),
                parse_mode=ParseMode.HTML,
            )
            await db.mark_reminder_sent(u["user_id"], today_local)
            log.info("lembrete enviado pra user_id=%s (%02d:%02d local)",
                     u["user_id"], hour, minute)
        except Exception:
            log.exception("falha enviando lembrete user_id=%s", u.get("user_id"))


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


def main() -> None:
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

    log.info("Bot rodando. Visão: %s | Chat: %s",
             os.environ.get("OPENROUTER_MODEL"),
             os.environ.get("OPENROUTER_CHAT_MODEL", "google/gemma-4-31b-it"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
