"""NLU layer determinística + tool-set restriction.

Roda ANTES do agente, decide o intent e restringe quais tools o LLM pode chamar.
Princípio: tirar a tool errada do menu é mais forte que regra de prompt.

Threshold 0.7 — abaixo, libera todas as tools (fallback seguro).
Sempre roda image_classifier quando há foto (decisão do user: sem heurísticas).
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from agent import image_classifier

log = logging.getLogger("agent.router")

Intent = Literal[
    "log_meal_now",
    "log_template",
    "choose_from_menu",
    "edit_meal",
    "log_weight",
    "log_exercise",
    "query_status",
    "onboarding",
    "confirm_pending",
    "cancel_pending",
    "free_chat",
]

CONFIDENCE_THRESHOLD = 0.7


@dataclass
class RoutingDecision:
    intent: Intent
    allowed_tools: set[str] | None   # None = todas (fallback)
    system_addendum: str             # micro-instrução pro turno, opcional
    confidence: float
    debug: dict = field(default_factory=dict)


# Mapa canônico — fácil revisar/expandir
INTENT_TOOL_SETS: dict[str, set[str]] = {
    "log_meal_now": {
        "estimate_meal_from_photo", "propose_meal", "log_meal",
        "search_foods", "search_vitat", "fetch_vitat_food",
        "get_food_portions", "save_food_alias",
        "save_meal_template",
        "get_user_profile", "get_calorie_balance",
        "generate_daily_chart",
    },
    "confirm_pending": {
        "confirm_proposal", "cancel_proposal",
    },
    "cancel_pending": {
        "cancel_proposal",
    },
    "edit_meal": {
        # Editar refeição já logada. SEM propose_meal/log_meal — evita
        # o bug de "registre como 21h" criar duplicata em vez de mover.
        "edit_meal",
        "get_recent_meals", "get_today_summary",
        "get_calorie_balance", "get_user_profile",
        "generate_daily_chart",
    },
    "log_template": {
        "list_meal_templates", "log_template",
        "get_user_profile", "get_calorie_balance",
        "generate_daily_chart",
    },
    "choose_from_menu": {
        # Explicitamente SEM estimate_meal_from_photo e SEM log_meal:
        # não queremos que o agente confunda "ajude a escolher" com "loga isso"
        "parse_menu",
        "search_foods", "search_vitat", "fetch_vitat_food", "get_food_portions",
        "get_user_profile", "get_calorie_balance",
        "remember",
    },
    "log_weight": {
        "parse_scale_photo", "log_weight",
        "compute_daily_goal", "get_user_profile",
        "generate_weight_chart",
    },
    "log_exercise": {
        "parse_watch_photo", "log_exercise",
        "get_calorie_balance", "get_user_profile",
        "get_exercises_today",
    },
    "query_status": {
        "get_today_summary", "get_calorie_balance",
        "get_recent_meals", "get_user_profile",
        "get_exercises_today",
        "generate_daily_chart", "generate_weight_chart",
        "generate_report_chart", "get_period_summary",
    },
    "onboarding": {
        "set_profile", "compute_daily_goal", "log_weight",
        "get_user_profile", "get_bot_capabilities",
    },
    "free_chat": set(),  # vira None na decisão final = libera todas
}


# Micro-prompts injetados SÓ no turno onde o intent for detectado
INTENT_ADDENDUMS: dict[str, str] = {
    "log_template": (
        "[Intent: log_template] Usuário quer logar uma refeição SALVA (template). "
        "Use list_meal_templates pra ver as opções, match o nome, e chame log_template. "
        "Se ambíguo (2+ templates batem), pergunte qual. Sem tools de busca de alimentos."
    ),
    "choose_from_menu": (
        "[Intent: choose_from_menu] Usuário quer AJUDA pra escolher do cardápio. "
        "Use parse_menu pra ler. NUNCA chame log_meal — ele NÃO comeu ainda. "
        "Filtre pelo critério dele (calorias, proteína, preferências), devolva "
        "2-4 sugestões ranqueadas, pergunte qual escolheu."
    ),
    "log_meal_now": (
        "[Intent: log_meal_now] Usuário acabou de comer / quer registrar refeição. "
        "Use estimate_meal_from_photo (se há foto) ou parse o texto. "
        "Confirme items com o user antes de chamar log_meal."
    ),
    "log_weight": (
        "[Intent: log_weight] Usuário registrando peso. parse_scale_photo (se foto) "
        "→ log_weight. Auto-recalcula meta. Responda direto, sem rodeio."
    ),
    "log_exercise": (
        "[Intent: log_exercise] Usuário registrando treino. parse_watch_photo (se foto) "
        "→ log_exercise. Confirme dados (atividade, kcal, duração) antes de salvar."
    ),
    "query_status": (
        "[Intent: query_status] Usuário quer status do dia/semana. "
        "Use get_calorie_balance + get_today_summary. Responda direto."
    ),
    "confirm_pending": (
        "[Intent: confirm_pending] Existe proposta pendente E o user confirmou. "
        "Chame confirm_proposal() — ele lê os dados exatos do scratchpad e loga. "
        "NÃO re-busque alimentos, NÃO recalcule. Após confirmar, escreva uma "
        "linha curta tipo '✅ Logado'."
    ),
    "cancel_pending": (
        "[Intent: cancel_pending] User cancelou a proposta pendente. "
        "Chame cancel_proposal() e responda 'Ok, cancelei.'."
    ),
    "edit_meal": (
        "[Intent: edit_meal] User quer EDITAR uma refeição JÁ logada — "
        "tipicamente mover horário ('registra como 21h de ontem'). "
        "Identifique o(s) meal_id(s) — o bot acabou de listar com #N — "
        "e chame edit_meal(meal_id=N, eaten_at=ISO) pra cada um. "
        "NUNCA chame propose_meal/log_meal aqui — você só está mexendo "
        "em refeições existentes. Se faltar o #N e não vier no contexto, "
        "use get_recent_meals pra descobrir, ou peça o ID ao user."
    ),
}


# ============================================================
# Classificação de texto (determinística, sem LLM)
# ============================================================
_KEYWORDS_MENU = re.compile(
    r"\b(escolh|sugere|sugest|recomend|qual.*comer|o que.*pedir|"
    r"posso.*comer|me ajud[ae].*escolh|card[áa]pio|menu|carta)\b",
    re.IGNORECASE,
)
_KEYWORDS_LOG_MEAL = re.compile(
    r"\b(comi|almoc(ei|ando)|jantei|tomei.*caf[éeê]|"
    r"loga.*refei|registra.*comi|acabei.*de.*comer)\b",
    re.IGNORECASE,
)
# "tomei meu whey", "comi minha marmita", "fiz meu shake", "repete o X"
_KEYWORDS_TEMPLATE = re.compile(
    r"\b("
    r"(tomei|comi|fiz|peguei|preparei|registra)\s+(meu|minha|o|a|aquele|aquela)\s+\w+"
    r"|repet(e|ir)\s+\w+"
    r"|loga\s+(meu|minha)\s+\w+"
    r")\b",
    re.IGNORECASE,
)
_KEYWORDS_STATUS = re.compile(
    r"\b(como.*t[áa]|quanto.*falta|balanco|balanço|sobrou|j[áa] comi|"
    r"resumo|gr[áa]fico|relat[óo]rio)\b",
    re.IGNORECASE,
)
_KEYWORDS_EXERCISE = re.compile(
    r"\b(corri|treinei|fui correr|academia|km|min(uto)?s?|treino|"
    r"caminhei|andei.*bike|musculaç[ãa]o)\b",
    re.IGNORECASE,
)
_KEYWORDS_ONBOARDING = re.compile(
    r"\b(definir.*meta|quero.*meta|come[çc]ar|configurar.*perfil|onboarding)\b",
    re.IGNORECASE,
)

# Edição de refeição já logada. Dois sinais:
#  (1) verbos explícitos de edição ("muda", "move", "edita", "altera", "corrige")
#  (2) "registra/registre/loga como X" — ambíguo sozinho; só conta como edit se
#      o bot acabou de mostrar IDs de refeição (#12, #15...) no turno anterior
_KEYWORDS_EDIT_STRONG = re.compile(
    r"\b("
    r"mov(e|er|a|i)|muda(r|nça)?|alter(a|ar|e)|edita(r)?|edite|"
    r"corrig(e|ir|i)|atualiz(a|ar|e)|"
    r"troc(a|ar|e).*(hor[áa]ri|data|comida|item|porç)"
    r")\b",
    re.IGNORECASE,
)
_KEYWORDS_EDIT_WEAK = re.compile(
    r"\b(registra|registre|registrar|loga|logar|salva|salvar)\s+como\b",
    re.IGNORECASE,
)
# Regex pra detectar se a última msg do bot citou IDs de refeição (#12, #15)
# ou listou "Pizza X — atualmente registrada em Y" etc.
_BOT_LISTED_MEALS = re.compile(
    r"(#\d+|atualmente\s+registrada|refei[çc][ãa]o\s+#?\d+)",
    re.IGNORECASE,
)
_WEIGHT_ONLY = re.compile(r"^\s*\d{2,3}([.,]\d)?\s*(kg)?\s*$", re.IGNORECASE)

# Confirmação / cancelamento de proposta pendente. Match curto/exato pra não
# falsa-positivar em frases longas ("sim, e além disso comi X" não é confirm).
_KEYWORDS_CONFIRM = re.compile(
    r"^\s*("
    r"sim|s|ok|okay|isso|certo|confirma|confirmo|confirmar|"
    r"pode|pode\s+ser|pode\s+logar|pode\s+salvar|"
    r"loga|logar|salva|salvar|registra|registrar|"
    r"t[áa]\s+(bom|certo|ok)|beleza|blz|valeu|"
    r"manda\s+bala|vai|✅"
    r")\s*[.!👍✅]?\s*$",
    re.IGNORECASE,
)
_KEYWORDS_CANCEL = re.compile(
    r"^\s*("
    r"n[ãa]o|nao|n|cancela|cancelar|errado|errei|deixa\s+pra\s+l[áa]|"
    r"esquece|ignora|❌|👎"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)


def classify_text(text: str, has_pending: bool = False,
                  bot_listed_meals: bool = False) -> tuple[Intent | None, float]:
    """Retorna (intent_sugerido, confiança) só por texto. None = não decidiu.

    has_pending: se True (há pending_proposal no scratchpad), 'sim/não' viram
    intents fortes de confirm/cancel. Sem pending, 'sim' isolado é ambíguo demais.

    bot_listed_meals: se True (a última msg do bot citou IDs de refeição ou
    'atualmente registrada em'), então 'registre como X' vira edit_meal —
    isso resolve o caso 'pizza registre como 9 da noite de ontem' depois do
    bot ter listado a pizza com horário errado."""
    if not text or not text.strip():
        return None, 0.0
    s = text.strip()
    # Confirmação/cancelamento têm prioridade quando há algo pendente
    if has_pending:
        if _KEYWORDS_CONFIRM.match(s):
            return "confirm_pending", 0.95
        if _KEYWORDS_CANCEL.match(s):
            return "cancel_pending", 0.92
    # Edição de refeição já logada
    if _KEYWORDS_EDIT_STRONG.search(s):
        return "edit_meal", 0.85
    if bot_listed_meals and _KEYWORDS_EDIT_WEAK.search(s):
        # "registre como 21h" sozinho é ambíguo; com contexto vira edit
        return "edit_meal", 0.85
    if _WEIGHT_ONLY.match(s):
        return "log_weight", 0.95
    if _KEYWORDS_ONBOARDING.search(s):
        return "onboarding", 0.85
    if _KEYWORDS_MENU.search(s):
        return "choose_from_menu", 0.85
    if _KEYWORDS_STATUS.search(s):
        return "query_status", 0.80
    if _KEYWORDS_EXERCISE.search(s):
        return "log_exercise", 0.75
    # Template tem que vir ANTES de log_meal porque "comi minha marmita"
    # bateria nos dois — preferimos o template (mais específico)
    if _KEYWORDS_TEMPLATE.search(s):
        return "log_template", 0.8
    if _KEYWORDS_LOG_MEAL.search(s):
        return "log_meal_now", 0.75
    return None, 0.0


# ============================================================
# Combinação de sinais (texto + imagem + contexto conversa)
# ============================================================
def _last_bot_intent(history: list[dict]) -> Intent | None:
    """Olha a última mensagem do assistant pra inferir o que ele acabou de pedir.
    Heurística leve — só pra reforçar inferência de imagem isolada."""
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = (msg.get("content") or "").lower()
        if not content:
            continue
        if "peso" in content and ("balan" in content or "pesa" in content):
            return "log_weight"
        if "treino" in content or "rel[óo]gio" in content:
            return "log_exercise"
        if "card[áa]pio" in content or "menu" in content:
            return "choose_from_menu"
        return None  # primeira msg do bot que não bate — não infere mais
    return None


def _combine(
    text_intent: Intent | None,
    text_conf: float,
    image_kind: str | None,
    image_conf: float,
    bot_last_intent: Intent | None,
) -> tuple[Intent, float]:
    """Regra explícita de combinação. Retorna (intent_final, confiança_final)."""

    # CASOS COM FOTO
    if image_kind and image_conf >= 0.6:
        if image_kind == "menu":
            # Foto de cardápio: SEMPRE choose_from_menu, independente do texto
            # (resolve Bug A e B: texto descritivo + foto de menu NÃO é log_meal)
            return "choose_from_menu", max(0.9, image_conf)

        if image_kind == "plate":
            # Texto sugere escolher? Trata como ambíguo → fallback seguro
            if text_intent == "choose_from_menu":
                return "free_chat", 0.5  # libera tudo
            # Default: logar refeição
            return "log_meal_now", max(0.85, image_conf)

        if image_kind == "scale":
            return "log_weight", max(0.9, image_conf)

        if image_kind == "watch":
            return "log_exercise", max(0.9, image_conf)

        # image=other — sinal fraco, usa texto se houver
        if text_intent:
            return text_intent, text_conf * 0.8

    # CASOS SEM FOTO (ou foto não classificada)
    if text_intent:
        return text_intent, text_conf

    # Bot acabou de pedir algo → assume contexto
    if bot_last_intent:
        return bot_last_intent, 0.7

    return "free_chat", 0.3


# ============================================================
# API pública
# ============================================================
def _last_bot_listed_meals(history: list[dict]) -> bool:
    """True se a última msg do bot citou IDs de refeição (#12) ou
    'atualmente registrada em' etc — indicador de que o user pode estar
    se referindo a refeições JÁ logadas no próximo turno."""
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or ""
        if not content:
            continue
        return bool(_BOT_LISTED_MEALS.search(content))
    return False


async def classify(
    text: str,
    photo_bytes: bytes | None,
    history: list[dict],
    vision_model: str,
    pending_kind: str | None = None,
) -> RoutingDecision:
    """Combina sinais de texto + imagem e devolve RoutingDecision.

    pending_kind: 'meal'|'exercise'|'weight'|None. Quando há proposta pendente
    no scratchpad, ajustes:
      - 'sim/não' isolado vira confirm_pending/cancel_pending com conf alta
      - foto isolada sem texto NÃO interrompe a confirmação (user pode mandar
        algo não relacionado, mas se é uma resposta curta confirmatória, vence)
    """
    has_pending = pending_kind is not None
    bot_listed_meals = _last_bot_listed_meals(history or [])
    text_intent, text_conf = classify_text(
        text or "",
        has_pending=has_pending,
        bot_listed_meals=bot_listed_meals,
    )

    image_kind, image_conf = (None, 0.0)
    if photo_bytes:
        image_kind, image_conf = await image_classifier.classify(photo_bytes, vision_model)

    bot_last = _last_bot_intent(history or [])

    intent, conf = _combine(text_intent, text_conf, image_kind, image_conf, bot_last)

    if conf >= CONFIDENCE_THRESHOLD:
        allowed = INTENT_TOOL_SETS.get(intent)
        if allowed is not None and not allowed:  # set vazio (free_chat) → libera
            allowed = None
        addendum = INTENT_ADDENDUMS.get(intent, "")
    else:
        allowed = None
        addendum = ""

    decision = RoutingDecision(
        intent=intent,
        allowed_tools=allowed,
        system_addendum=addendum,
        confidence=conf,
        debug={
            "text_intent": text_intent,
            "text_conf": text_conf,
            "image_kind": image_kind,
            "image_conf": image_conf,
            "bot_last": bot_last,
            "bot_listed_meals": bot_listed_meals,
            "pending_kind": pending_kind,
        },
    )
    log.info(
        "[router] intent=%s conf=%.2f allowed=%s (text=%s img=%s pending=%s)",
        intent, conf,
        f"{len(allowed)} tools" if allowed else "ALL",
        text_intent, image_kind, pending_kind,
    )
    return decision
