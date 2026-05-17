import html
import io
import json
import logging
import os

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

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("health-chat")

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"]) if os.getenv("ALLOWED_USER_ID") else None


def _esc(s) -> str:
    return html.escape(str(s))


def _guard(update: Update) -> bool:
    if ALLOWED_USER_ID is None:
        return True
    u = update.effective_user
    return u is not None and u.id == ALLOWED_USER_ID


def _current_model(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.bot_data.get("model", os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"))


def _source_emoji(source: str) -> str:
    return {"TACO": "✅", "estimativa": "🟡", "sem dados": "❌"}.get(source, "•")


def _format_meal_message(meal_id: int, items: list[dict], totals: dict, model: str, notes: str | None) -> str:
    lines = []
    for i, it in enumerate(items):
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
    # botão de correção por item (até 8)
    for i, it in enumerate(items[:8]):
        if it.get("alternatives"):
            rows.append([InlineKeyboardButton(
                f"✏️ trocar #{i+1} {it['name_llm'][:25]}",
                callback_data=f"fix:{meal_id}:{i}",
            )])
    rows.append([InlineKeyboardButton("🗑 apagar refeição", callback_data=f"del:{meal_id}")])
    return InlineKeyboardMarkup(rows)


# ----------------- comandos -----------------


async def cmd_start(update: Update, _) -> None:
    u = update.effective_user
    log.info("start from user_id=%s username=%s", u.id, u.username)
    n = await db.count_foods()
    await update.message.reply_text(
        f"Oi! Seu user_id é {u.id}.\n"
        f"Base nutricional carregada: {n} alimentos (TACO).\n\n"
        "Manda uma foto que eu estimo macros usando a base.\n"
        "Comandos: /hoje /semana /apagar /modelo /buscar /ajuda"
    )


async def cmd_ajuda(update: Update, _) -> None:
    if not _guard(update): return
    await update.message.reply_text(
        "📷 Foto → identifico itens, busco no TACO, calculo macros\n"
        "✅ = match TACO  ·  🟡 = estimativa LLM  ·  ❌ = sem dados\n\n"
        "/hoje — refeições e total do dia\n"
        "/semana — total dos últimos 7 dias\n"
        "/apagar — remove última refeição\n"
        "/modelo [slug] — vê/troca modelo de visão\n"
        "/buscar <termo> — procura no banco TACO\n"
    )


async def cmd_modelo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    if context.args:
        new = " ".join(context.args).strip()
        context.bot_data["model"] = new
        await update.message.reply_text(f"Modelo agora: {new}")
    else:
        await update.message.reply_text(f"Modelo atual: {_current_model(context)}")


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
    meals = await db.list_today(update.effective_user.id)
    if not meals:
        await update.message.reply_text("Nada registrado hoje.")
        return
    lines = []
    tot_k = tot_p = tot_c = tot_f = 0.0
    for m in meals:
        items = m["items"] if isinstance(m["items"], list) else json.loads(m["items"])
        names = ", ".join(i.get("food_name") or i["name_llm"] for i in items[:3])
        lines.append(f"#{m['id']}  {m['eaten_at'].strftime('%H:%M')}  {_esc(names)}  — {float(m['kcal']):.0f} kcal")
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


# ----------------- foto / callbacks -----------------


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _guard(update): return
    msg = update.message
    await msg.chat.send_action(ChatAction.TYPING)

    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    image_bytes = buf.getvalue()

    model = _current_model(context)
    try:
        analysis, _raw = await llm.identify_items(image_bytes, "image/jpeg", model)
    except Exception as e:
        log.exception("vision LLM failed")
        await msg.reply_text(f"Falha na visão: {e}")
        return

    llm_items = analysis.get("items", [])
    if not llm_items:
        await msg.reply_text("Não identifiquei comida na foto.")
        return

    await msg.chat.send_action(ChatAction.TYPING)
    try:
        resolved, totals = await calculator.calc_meal(llm_items)
    except Exception as e:
        log.exception("calc failed")
        await msg.reply_text(f"Falha no cálculo: {e}")
        return

    meal_id = await db.insert_meal(
        user_id=update.effective_user.id,
        vision_model=model,
        photo_file_id=photo.file_id,
        items=resolved,
        totals=totals,
        notes=analysis.get("notes"),
    )

    await msg.reply_text(
        _format_meal_message(meal_id, resolved, totals, model, analysis.get("notes")),
        parse_mode=ParseMode.HTML,
        reply_markup=_meal_keyboard(meal_id, resolved),
    )


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


async def on_text(update: Update, _) -> None:
    if not _guard(update): return
    await update.message.reply_text("Manda uma foto. /ajuda pra comandos.")


async def _post_init(app: Application) -> None:
    await db.init()
    n = await db.count_foods()
    if n == 0:
        log.warning("Banco vazio! Rode: python import_taco.py")
    else:
        log.info("DB pronto. %s alimentos carregados.", n)


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
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot rodando.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
