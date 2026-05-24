"""Lógica dos comandos slash, agnóstica de canal.

bot.py (Telegram) e whatsapp.py (Twilio) chamam as mesmas funções daqui pra
manter UX consistente entre os canais — sem duplicar formatação/consulta.

Cada handler:
- recebe (user_id: int, args: list[str], **kwargs opcionais)
- retorna CommandResult(text=HTML, png=bytes|None)

O texto vem em HTML do Telegram (tags <b>, <i>, <code>, etc) — cada canal
converte pra sua sintaxe (Telegram renderiza direto; WhatsApp converte
pra *bold*/_italic_).
"""
from __future__ import annotations

import html as _html
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import db
import matcher

log = logging.getLogger("health-chat.commands")


@dataclass
class Suggestion:
    """Sugestão clicável anexada a uma mensagem.

    Renderiza diferente por canal:
    - Telegram: vira InlineKeyboardButton com callback_data
    - WhatsApp (sandbox/hoje): vira linha extra de texto "💡 Atalhos: /cmd1 · /cmd2"
    - WhatsApp (produção/futuro): vira Quick Reply Button via Content Template

    label: texto curto pro botão (max ~20 chars)
    command: o slash command (com args) que deve rodar ao clicar.
             Ex: '/apagar 12', '/trocar 12 1'.
    """
    label: str
    command: str


@dataclass
class CommandResult:
    """Resultado de um comando, agnóstico de canal."""
    text: str | None = None                # HTML do Telegram (canal converte)
    png: bytes | None = None               # imagem opcional (gráficos)
    suggestions: list[Suggestion] | None = None  # ações sugeridas (botões)


def _esc(s) -> str:
    return _html.escape(str(s))


# ============================================================
# /ajuda — texto compartilhado entre canais
# ============================================================

COMMANDS_HELP_TELEGRAM = """\
🎯 <b>Botões fixos embaixo</b> — atalhos pras ações comuns:
  🍽 Refeição · ⚖️ Peso · 🏃 Treino · 📊 Hoje · 🎯 Meta · ⚙️ Mais

💬 <b>Ou escreva livre</b>: 'comi 100g arroz e bife', 'como tá meu dia?',
'foto do cardápio, quero algo com carne', '101.8' (peso direto).

📷 <b>Mande foto direta</b> — identifico prato, cardápio, relógio ou balança.

<b>Todos os comandos (também acessíveis via botões):</b>
/start /ajuda — esta mensagem
/perfil — perfil e meta
/hoje /ontem — refeições do dia
/dia [data] — refeições de outro dia (ex: /dia 23/05, /dia anteontem)
/semana — total dos últimos 7 dias
/grafico — anel kcal + macros + refeições
/relatorio [semana|mes|N] — gráfico do período
/lembrete [off|on|HH:MM] — lembrete diário de pesagem (ex: 6:30, 7, 06:35)
/apagar — remove última refeição
/buscar &lt;termo&gt; — busca alimento
/reset — nova conversa

<b>Marcadores:</b>
✅ TACO   🌿 Vitat   🟡 estimativa   ❌ sem dados
"""


COMMANDS_HELP_WHATSAPP = """\
💬 *Fale livre*: 'comi 100g arroz e bife', 'como tá meu dia?',
'foto do cardápio, quero algo com carne', '101.8' (peso direto).

📷 *Mande foto* — identifico prato, cardápio, relógio ou balança.

*Comandos:*
/start /ajuda — esta mensagem
/perfil — perfil e meta
/hoje /semana — totais do período
/grafico — gráfico do dia
/relatorio [semana|mes|N] — gráfico do período
/lembrete [off|on|HH:MM] — lembrete diário (ex: 6:30, 7, 06:35)
/apagar — remove última refeição
/buscar <termo> — busca alimento
/reset — nova conversa


*Marcadores:*
✅ TACO   🌿 Vitat   🟡 estimativa   ❌ sem dados
"""


async def cmd_ajuda(user_id: int, args: list[str]) -> CommandResult:
    return CommandResult(text=COMMANDS_HELP_TELEGRAM)


async def cmd_start(user_id: int, args: list[str]) -> CommandResult:
    n = await db.count_foods()
    text = (
        f"Oi! Seu user_id é <code>{user_id}</code>. Base nutricional: {n} alimentos.\n\n"
        f"{COMMANDS_HELP_TELEGRAM}\n"
        "👉 Sem perfil ainda? Diga <i>'quero definir minha meta'</i> pra começar o onboarding."
    )
    return CommandResult(text=text)


# ============================================================
# /perfil
# ============================================================

async def cmd_perfil(user_id: int, args: list[str]) -> CommandResult:
    p = await db.get_profile(user_id)
    if not p:
        return CommandResult(text=(
            "Sem perfil ainda. Manda uma mensagem tipo 'quero definir minha meta' "
            "que eu te conduzo no onboarding."
        ))
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
    return CommandResult(text="\n".join(lines))


# ============================================================
# /hoje
# ============================================================

async def cmd_hoje(user_id: int, args: list[str]) -> CommandResult:
    from zoneinfo import ZoneInfo
    profile = await db.get_profile(user_id) or {}
    tz_name = profile.get("timezone") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")
    meals = await db.list_today(user_id)
    if not meals:
        return CommandResult(text="Nada registrado hoje.")
    lines = []
    tot_k = tot_p = tot_c = tot_f = 0.0
    for m in meals:
        items = m["items"] if isinstance(m["items"], list) else json.loads(m["items"])
        names = ", ".join(i.get("food_name") or i["name_llm"] for i in items[:3])
        local_time = m["eaten_at"].astimezone(tz).strftime("%H:%M")
        lines.append(f"#{m['id']}  {local_time}  {_esc(names)}  — {float(m['kcal']):.0f} kcal")
        tot_k += float(m["kcal"]); tot_p += float(m["protein_g"])
        tot_c += float(m["carbs_g"]); tot_f += float(m["fat_g"])
    text = (
        f"<b>Hoje</b> ({len(meals)} refeições)\n" + "\n".join(lines) +
        f"\n\n🔥 <b>{tot_k:.0f} kcal</b>\nP {tot_p:.0f}  C {tot_c:.0f}  G {tot_f:.0f}"
    )
    suggestions = [
        Suggestion(label="📊 Gráfico", command="/grafico"),
        Suggestion(label="🗑 Apagar última", command="/apagar"),
    ]
    return CommandResult(text=text, suggestions=suggestions)


# ============================================================
# /dia e /ontem — refeições de um dia específico (retroativo)
# ============================================================

async def cmd_dia(user_id: int, args: list[str]) -> CommandResult:
    """`/dia` (sem args) = hoje. `/dia ontem`, `/dia 23/05`, `/dia 2026-05-23`."""
    from zoneinfo import ZoneInfo
    from agent.time_parse import parse_date_pt
    profile = await db.get_profile(user_id) or {}
    tz_name = profile.get("timezone") or "America/Sao_Paulo"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")
    today_local = datetime.now(tz).date()

    if not args:
        target = today_local
    else:
        target = parse_date_pt(" ".join(args), ref_today=today_local)
        if target is None:
            return CommandResult(text=(
                "Não entendi a data. Exemplos:\n"
                "  <code>/dia</code> — hoje\n"
                "  <code>/dia ontem</code>\n"
                "  <code>/dia anteontem</code>\n"
                "  <code>/dia 23/05</code>\n"
                "  <code>/dia 2026-05-23</code>"
            ))

    meals = await db.list_meals_on_date(user_id, target, tz_name)
    exercises = await db.list_exercises_on_date(user_id, target, tz_name)

    label_date = target.strftime("%d/%m/%Y")
    if target == today_local:
        label = f"Hoje ({label_date})"
    elif target == today_local - timedelta(days=1):
        label = f"Ontem ({label_date})"
    elif target == today_local - timedelta(days=2):
        label = f"Anteontem ({label_date})"
    else:
        label = label_date

    if not meals and not exercises:
        return CommandResult(text=f"<b>{label}</b>\nNada registrado.")

    lines = [f"<b>{label}</b>"]
    tot_k = tot_p = tot_c = tot_f = 0.0
    if meals:
        lines.append(f"\n🍽 <b>Refeições ({len(meals)})</b>")
        for m in meals:
            items = m["items"] if isinstance(m["items"], list) else json.loads(m["items"])
            names = ", ".join(i.get("food_name") or i["name_llm"] for i in items[:3])
            local_time = m["eaten_at"].astimezone(tz).strftime("%H:%M")
            lines.append(
                f"  #{m['id']}  {local_time}  {_esc(names)}  — {float(m['kcal']):.0f} kcal"
            )
            tot_k += float(m["kcal"]); tot_p += float(m["protein_g"])
            tot_c += float(m["carbs_g"]); tot_f += float(m["fat_g"])
        lines.append(
            f"  🔥 <b>{tot_k:.0f} kcal</b> · "
            f"P {tot_p:.0f}  C {tot_c:.0f}  G {tot_f:.0f}"
        )

    if exercises:
        tot_burn = sum(int(e["kcal_burned"]) for e in exercises)
        lines.append(f"\n🏃 <b>Exercícios ({len(exercises)})</b>")
        for e in exercises:
            local_time = e["done_at"].astimezone(tz).strftime("%H:%M")
            dur = f", {e['duration_min']}min" if e.get("duration_min") else ""
            lines.append(
                f"  #{e['id']}  {local_time}  {_esc(e['activity'])}{dur}  "
                f"— {e['kcal_burned']} kcal"
            )
        lines.append(f"  🔥 Queimadas: <b>{tot_burn}</b> kcal")

    suggestions = []
    if target == today_local:
        suggestions = [
            Suggestion(label="📊 Gráfico", command="/grafico"),
            Suggestion(label="🗑 Apagar última", command="/apagar"),
        ]
    else:
        # Pra dias passados, sugere mostrar gráfico de outro período
        suggestions = [Suggestion(label="📈 Relatório semana", command="/relatorio semana")]

    return CommandResult(text="\n".join(lines), suggestions=suggestions)


async def cmd_ontem(user_id: int, args: list[str]) -> CommandResult:
    """Atalho pra /dia ontem."""
    return await cmd_dia(user_id, ["ontem"])


# ============================================================
# /semana
# ============================================================

async def cmd_semana(user_id: int, args: list[str]) -> CommandResult:
    s = await db.summary_since(user_id, datetime.now(timezone.utc) - timedelta(days=7))
    if not s["n"]:
        return CommandResult(text="Sem refeições nos últimos 7 dias.")
    text = (
        f"<b>Últimos 7 dias</b> — {s['n']} refeições\n"
        f"Total: {float(s['kcal']):.0f} kcal\n"
        f"Média/dia: {float(s['kcal'])/7:.0f} kcal\n"
        f"P {float(s['protein_g']):.0f}  C {float(s['carbs_g']):.0f}  G {float(s['fat_g']):.0f}"
    )
    return CommandResult(text=text)


# ============================================================
# /apagar
# ============================================================

async def cmd_apagar(user_id: int, args: list[str]) -> CommandResult:
    d = await db.delete_last(user_id)
    return CommandResult(text=f"Removido #{d}." if d else "Nada pra apagar.")


# ============================================================
# /grafico
# ============================================================

async def cmd_grafico(user_id: int, args: list[str]) -> CommandResult:
    from agent.tools import _build_daily_chart
    try:
        png = await _build_daily_chart(user_id)
    except Exception as e:
        log.exception("erro gerando gráfico")
        return CommandResult(text=f"Falha gerando gráfico: {e}")
    return CommandResult(png=png)


# ============================================================
# /relatorio
# ============================================================

async def cmd_relatorio(user_id: int, args: list[str]) -> CommandResult:
    from agent import charts
    arg = (args[0].lower() if args else "semana")
    days = 30 if arg in ("mes", "mês", "30") else 7
    if arg.isdigit():
        days = max(1, min(90, int(arg)))

    summary = await db.period_summary(user_id, days)
    p = await db.get_profile(user_id)
    goal = p.get("daily_kcal") if p else None

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
    png = charts.daily_intake_vs_goal(
        summary["per_day"], goal_kcal=goal, title=f"Últimos {days} dias"
    )
    return CommandResult(text=text, png=png)


# ============================================================
# /lembrete
# ============================================================

def parse_hhmm(s: str) -> tuple[int, int] | None:
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


async def cmd_lembrete(user_id: int, args: list[str]) -> CommandResult:
    p = await db.get_profile(user_id) or {}
    if not args:
        enabled = p.get("weigh_in_enabled", True)
        hour = p.get("weigh_in_hour", 6) or 6
        minute = p.get("weigh_in_minute", 0) or 0
        status = "✅ ligado" if enabled else "❌ desligado"
        text = (
            f"Lembrete de pesagem: {status} às <b>{hour:02d}:{minute:02d}</b> "
            f"({p.get('weigh_in_tz','America/Sao_Paulo')})\n\n"
            "Uso:\n"
            "  <code>/lembrete off</code>  — desliga\n"
            "  <code>/lembrete on</code>   — liga\n"
            "  <code>/lembrete 7</code>    — 07:00\n"
            "  <code>/lembrete 6:30</code> — 06:30\n"
            "  <code>/lembrete 06:35</code> — 06:35"
        )
        return CommandResult(text=text)

    arg = args[0].lower()
    if arg == "off":
        await db.upsert_profile_field(user_id, "weigh_in_enabled", False)
        return CommandResult(text="Lembrete desligado.")
    if arg == "on":
        await db.upsert_profile_field(user_id, "weigh_in_enabled", True)
        h = p.get("weigh_in_hour", 6) or 6
        m = p.get("weigh_in_minute", 0) or 0
        return CommandResult(text=f"Lembrete ligado às {h:02d}:{m:02d}.")

    parsed = parse_hhmm(arg)
    if parsed is None:
        return CommandResult(text=(
            "Formato inválido. Use:\n"
            "  /lembrete 7        (07:00)\n"
            "  /lembrete 6:30     (06:30)\n"
            "  /lembrete off"
        ))

    h, m = parsed
    await db.upsert_profile_field(user_id, "weigh_in_hour", h)
    await db.upsert_profile_field(user_id, "weigh_in_minute", m)
    await db.upsert_profile_field(user_id, "weigh_in_enabled", True)
    return CommandResult(text=f"Lembrete configurado pra <b>{h:02d}:{m:02d}</b>.")


# ============================================================
# /reset
# ============================================================

async def cmd_reset(user_id: int, args: list[str]) -> CommandResult:
    await db.close_active_conversation(user_id)
    return CommandResult(text="Conversa zerada. 🔄 Próxima mensagem começa do zero.")


# ============================================================
# /buscar
# ============================================================

async def cmd_buscar(user_id: int, args: list[str]) -> CommandResult:
    if not args:
        return CommandResult(text="Uso: /buscar arroz integral")
    name = " ".join(args)
    m = await matcher.match_one(name)
    if not m.alternatives:
        return CommandResult(text="Nada encontrado.")
    lines = [f"Para '{_esc(name)}':"]
    for a in m.alternatives[:5]:
        marker = " ⭐" if a["id"] == m.food_id else ""
        lines.append(f"  • {_esc(a['name'])} (sim {a['score']:.2f}){marker}")
    lines.append(f"\nMétodo: {m.method}")
    return CommandResult(text="\n".join(lines))


# ============================================================
# Dispatcher
# ============================================================

# Mapa de nome → handler. bot.py e whatsapp.py usam isso pra rotear comandos.
COMMAND_HANDLERS = {
    "start": cmd_start,
    "ajuda": cmd_ajuda,
    "help": cmd_ajuda,
    "perfil": cmd_perfil,
    "hoje": cmd_hoje,
    "dia": cmd_dia,
    "ontem": cmd_ontem,
    "semana": cmd_semana,
    "apagar": cmd_apagar,
    "grafico": cmd_grafico,
    "relatorio": cmd_relatorio,
    "lembrete": cmd_lembrete,
    "reset": cmd_reset,
    "buscar": cmd_buscar,
}


def parse_slash(text: str) -> tuple[str, list[str]] | None:
    """Detecta '/comando arg1 arg2'. Retorna (cmd, args) ou None."""
    if not text or not text.startswith("/"):
        return None
    parts = text[1:].strip().split()
    if not parts:
        return None
    cmd = parts[0].lower()
    # Remove '@botname' do Telegram (ex: /hoje@MeuBot)
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd, parts[1:]
