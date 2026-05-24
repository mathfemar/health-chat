"""Parser de expressões temporais em PT-BR, sem dependência externa.

Reconhece o subset que importa pra logar refeição/treino retroativo:
  - 'agora', 'hoje', 'ontem', 'anteontem'
  - '19h', '19:30', '7h30', '07:35', 'às 19h'
  - 'ontem 19h', 'ontem às 19:30', 'anteontem 12h'
  - 'há 2 horas', 'faz 1 hora', '2 horas atrás', '30 min atrás'
  - '23/05', '23/05/2026', '2026-05-23' (com ou sem hora)
  - 'sábado 12h', 'segunda 19h' (última ocorrência ≤ hoje)

Retorna datetime aware no tz do user. None se não reconheceu.

Diferente de bibliotecas tipo dateparser:
  - 100% determinístico, ~80 linhas, sem rede / sem cache global
  - Em caso de ambiguidade ("hoje" sem hora), assume HORA ATUAL do user
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


_HORA_PT = r"(?:às?\s+)?(\d{1,2})(?::|h)(\d{2})?"   # 19h, 19:30, 7h30, às 19h
_HORA_ONLY = re.compile(rf"^\s*{_HORA_PT}\s*$", re.IGNORECASE)
_HA_HORAS = re.compile(
    r"(?:há|faz)\s+(\d+)\s*(min|minuto|minutos|h|hora|horas)\s*(atr[áa]s)?"
    r"|(\d+)\s*(min|minuto|minutos|h|hora|horas)\s+atr[áa]s",
    re.IGNORECASE,
)

_WEEKDAYS = {
    "segunda": 0, "seg": 0,
    "terça": 1, "terca": 1, "ter": 1,
    "quarta": 2, "qua": 2,
    "quinta": 3, "qui": 3,
    "sexta": 4, "sex": 4,
    "sábado": 5, "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b(segunda|seg|ter[çc]a|ter|quarta|qua|quinta|qui|sexta|sex|s[áa]bado|sab|domingo|dom)\b",
    re.IGNORECASE,
)

_DATE_DMY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
# Aceita YYYY-MM-DD seguido de qualquer não-dígito (T, espaço, fim) — pega ISO completo
_DATE_YMD = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
# Hora dentro de ISO 8601 (T19:00 ou T19:00:00): grupos = (h, m)
_ISO_HOUR = re.compile(r"T(\d{1,2}):(\d{2})(?::\d{2})?", re.IGNORECASE)


def _extract_hour(text: str) -> tuple[int, int] | None:
    """Pega a primeira hora válida (HH ou HH:MM ou HHhMM, inclusive ISO T19:00) do texto."""
    iso_m = _ISO_HOUR.search(text)
    if iso_m:
        h = int(iso_m.group(1)); mn = int(iso_m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return h, mn
    m = re.search(rf"(?<!\d){_HORA_PT}(?!\d)", text, re.IGNORECASE)
    if not m:
        return None
    h = int(m.group(1))
    mn = int(m.group(2)) if m.group(2) else 0
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return h, mn


def _last_weekday_on_or_before(ref: date, weekday: int) -> date:
    """weekday: 0=segunda ... 6=domingo. Retorna a data ≤ ref com esse weekday."""
    diff = (ref.weekday() - weekday) % 7
    return ref - timedelta(days=diff)


def parse_time_pt(text: str, user_tz: ZoneInfo | str, ref_now: datetime | None = None) -> datetime | None:
    """Parser principal. Retorna datetime aware no tz do user, ou None.

    text: expressão crua. Pode ser uma frase inteira ("ontem comi às 19h") —
          a função encontra a info temporal e ignora o resto.
    user_tz: ZoneInfo ou nome IANA.
    ref_now: datetime de referência (default = agora no tz do user). Útil pra testes.
    """
    if not text:
        return None
    if isinstance(user_tz, str):
        try:
            user_tz = ZoneInfo(user_tz)
        except Exception:
            user_tz = ZoneInfo("America/Sao_Paulo")
    if ref_now is None:
        ref_now = datetime.now(user_tz)
    else:
        ref_now = ref_now.astimezone(user_tz)

    s = text.strip().lower()

    # ===== relativos absolutos =====
    if re.search(r"\bagora\b", s):
        return ref_now
    if re.search(r"\banteontem\b", s):
        d = ref_now.date() - timedelta(days=2)
        return _combine_with_hour(d, s, user_tz, fallback_hour=ref_now.time())
    if re.search(r"\bontem\b", s):
        d = ref_now.date() - timedelta(days=1)
        return _combine_with_hour(d, s, user_tz, fallback_hour=ref_now.time())
    if re.search(r"\bhoje\b", s):
        return _combine_with_hour(ref_now.date(), s, user_tz, fallback_hour=ref_now.time())

    # ===== 'há N horas/minutos atrás' =====
    m = _HA_HORAS.search(s)
    if m:
        # Grupos 1+2 ('há 2 horas') ou 4+5 ('2 horas atrás')
        n = int(m.group(1) or m.group(4))
        unit = (m.group(2) or m.group(5)).lower()
        delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
        return ref_now - delta

    # ===== data explícita YYYY-MM-DD =====
    m = _DATE_YMD.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            target = date(y, mo, d)
        except ValueError:
            return None
        return _combine_with_hour(target, s, user_tz, fallback_hour=ref_now.time())

    # ===== data DD/MM ou DD/MM/AAAA =====
    m = _DATE_DMY.search(s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y_raw = m.group(3)
        if y_raw:
            y = int(y_raw)
            if y < 100:
                y += 2000
        else:
            y = ref_now.year
            # Se a data já passou e ela é "futuro" no ano atual, assume ano passado
            try:
                tentative = date(y, mo, d)
                if tentative > ref_now.date():
                    y -= 1
            except ValueError:
                return None
        try:
            target = date(y, mo, d)
        except ValueError:
            return None
        return _combine_with_hour(target, s, user_tz, fallback_hour=ref_now.time())

    # ===== weekday ('sábado 12h') =====
    wm = _WEEKDAY_RE.search(s)
    if wm:
        weekday = _WEEKDAYS[wm.group(1).lower().replace("ç", "c").replace("á", "a")]
        target = _last_weekday_on_or_before(ref_now.date(), weekday)
        # Se o weekday == hoje e nenhuma hora foi dita, presume última semana
        if target == ref_now.date() and _extract_hour(s) is None:
            target -= timedelta(days=7)
        return _combine_with_hour(target, s, user_tz, fallback_hour=ref_now.time())

    # ===== só hora ('19h', '19:30', 'às 19:30') =====
    hour_only = _HORA_ONLY.match(s)
    if hour_only:
        h = int(hour_only.group(1))
        mn = int(hour_only.group(2)) if hour_only.group(2) else 0
        if 0 <= h <= 23 and 0 <= mn <= 59:
            d = ref_now.date()
            cand = datetime.combine(d, time(h, mn), tzinfo=user_tz)
            # Se hora no futuro hoje, assume que foi ontem
            if cand > ref_now + timedelta(minutes=1):
                cand -= timedelta(days=1)
            return cand

    return None


def _combine_with_hour(target_date: date, text: str, tz: ZoneInfo,
                       fallback_hour: time) -> datetime:
    """Combina target_date com a hora extraída do texto. Se não houver hora,
    usa fallback_hour (geralmente hora atual)."""
    hm = _extract_hour(text)
    if hm:
        return datetime.combine(target_date, time(hm[0], hm[1]), tzinfo=tz)
    return datetime.combine(target_date, fallback_hour.replace(microsecond=0), tzinfo=tz)


def parse_date_pt(text: str, ref_today: date | None = None) -> date | None:
    """Versão simplificada que retorna só uma data (sem hora). Usado por /dia."""
    if not text:
        return None
    s = text.strip().lower()
    if ref_today is None:
        ref_today = date.today()
    if s in ("hoje",):
        return ref_today
    if s in ("ontem",):
        return ref_today - timedelta(days=1)
    if s in ("anteontem",):
        return ref_today - timedelta(days=2)
    m = _DATE_YMD.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE_DMY.search(s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y_raw = m.group(3)
        if y_raw:
            y = int(y_raw)
            if y < 100:
                y += 2000
        else:
            y = ref_today.year
            try:
                tentative = date(y, mo, d)
                if tentative > ref_today:
                    y -= 1
            except ValueError:
                return None
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    wm = _WEEKDAY_RE.search(s)
    if wm:
        weekday = _WEEKDAYS[wm.group(1).lower().replace("ç", "c").replace("á", "a")]
        target = _last_weekday_on_or_before(ref_today, weekday)
        if target == ref_today:
            target -= timedelta(days=7)
        return target
    return None
