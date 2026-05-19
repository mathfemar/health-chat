"""As 11 tools que o agente pode chamar.

Cada tool retorna dict serializável (JSON). ctx é passado pelo runtime e contém:
  ctx['user_id']       : int
  ctx['conv_id']       : int  (id da conversa atual)
  ctx['download_photo']: async callable (photo_ref: str) -> bytes | None
                         injetado pelo adapter (Telegram ou Twilio/WhatsApp)
  ctx['vision_model']  : slug do modelo de visão
"""
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import calculator
import db
import goals
import llm
import matcher
from agent import charts
from agent.registry import tool
from sources import vitat

log = logging.getLogger("agent.tools")


# --------------------------------------------------------------
# 1. search_foods (local: TACO + cache Vitat)
# --------------------------------------------------------------
@tool(
    name="search_foods",
    description=(
        "Busca alimentos no banco LOCAL (TACO + Vitat já cacheado). "
        "SEMPRE tente esta primeiro antes de search_vitat. Retorna até 5 hits com similarity."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Nome do alimento, ex: 'salmão grelhado'"},
        },
        "required": ["query"],
    },
)
async def search_foods(ctx: dict, query: str) -> dict:
    m = await matcher.match_one(query)
    return {
        "best_match": (
            {"food_id": m.food_id, "name": m.food_name, "score": round(m.score, 2), "method": m.method}
            if m.food_id else None
        ),
        "alternatives": [
            {"food_id": a["id"], "name": a["name"], "score": round(a["score"], 2)}
            for a in m.alternatives[:5]
        ],
    }


# --------------------------------------------------------------
# 2. search_vitat (online)
# --------------------------------------------------------------
@tool(
    name="search_vitat",
    description=(
        "Busca online no Vitat (vitat.com.br). Use APENAS quando search_foods "
        "retornar nada útil. Não use repetidamente — Vitat é mais lento."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def search_vitat_tool(ctx: dict, query: str) -> dict:
    try:
        hits = await vitat.search(query, max_results=6)
        log.info("vitat.search('%s') → %d hits", query, len(hits))
        return {"hits": [{"vitat_id": h.id, "name": h.name,
                          "default_measure": h.default_measure} for h in hits]}
    except Exception as e:
        log.exception("vitat.search falhou")
        return {"error": str(e), "hits": []}


# --------------------------------------------------------------
# 3. fetch_vitat_food (baixa + salva)
# --------------------------------------------------------------
@tool(
    name="fetch_vitat_food",
    description=(
        "Baixa detalhes nutricionais + porções de um alimento do Vitat e SALVA "
        "no banco local. Idempotente. Retorna o food_id local pra usar nas próximas chamadas."
    ),
    parameters={
        "type": "object",
        "properties": {
            "vitat_id": {"type": "integer"},
            "name_hint": {"type": "string", "description": "Nome retornado do search, ajuda no slug"},
        },
        "required": ["vitat_id"],
    },
)
async def fetch_vitat_food(ctx: dict, vitat_id: int, name_hint: str | None = None) -> dict:
    # checa se já temos cacheado
    pool = db.pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "select id, name from foods where source='VITAT' and source_id=$1", vitat_id
        )
        if existing:
            return {"food_id": existing["id"], "name": existing["name"], "cached": True}

    slug = None
    if name_hint:
        norm = matcher.normalize(name_hint).replace(" ", "-")
        slug = f"{vitat_id}-{norm}"

    try:
        vf = await vitat.fetch_food(vitat_id, slug_hint=slug)
    except Exception as e:
        log.exception("vitat.fetch_food falhou")
        return {"error": str(e)}

    async with pool.acquire() as conn:
        food_id = await conn.fetchval(
            """
            insert into foods (source, source_id, name, name_normalized, kcal, protein_g, carbs_g, fat_g, fiber_g)
            values ('VITAT', $1, $2, $3, $4, $5, $6, $7, $8)
            on conflict (source, source_id) do update set
                name = excluded.name, name_normalized = excluded.name_normalized,
                kcal = excluded.kcal, protein_g = excluded.protein_g,
                carbs_g = excluded.carbs_g, fat_g = excluded.fat_g, fiber_g = excluded.fiber_g
            returning id
            """,
            vitat_id, vf.name, matcher.normalize(vf.name),
            vf.kcal_100g, vf.protein_100g, vf.carbs_100g, vf.fat_100g, vf.fiber_100g,
        )
        # apaga porções anteriores e reinsere (idempotente)
        await conn.execute("delete from food_portions where food_id=$1", food_id)
        for p in vf.portions:
            await conn.execute(
                """
                insert into food_portions (food_id, name, name_normalized, grams, kcal, protein_g, carbs_g, fat_g)
                values ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                food_id, p.name, matcher.normalize(p.name), p.grams,
                p.kcal, p.protein_g, p.carbs_g, p.fat_g,
            )

    return {
        "food_id": food_id,
        "name": vf.name,
        "kcal_per_100g": round(vf.kcal_100g, 1),
        "portions_count": len(vf.portions),
        "cached": False,
    }


# --------------------------------------------------------------
# 4. get_food_portions
# --------------------------------------------------------------
@tool(
    name="get_food_portions",
    description=(
        "Lista porções nomeadas de um alimento (filé pequeno, posta média, 1 unidade, etc) "
        "com kcal/macros pra cada uma. Use quando o usuário disse uma medida caseira."
    ),
    parameters={
        "type": "object",
        "properties": {
            "food_id": {"type": "integer"},
        },
        "required": ["food_id"],
    },
)
async def get_food_portions(ctx: dict, food_id: int) -> dict:
    pool = db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select name, grams, kcal, protein_g, carbs_g, fat_g
            from food_portions where food_id=$1 order by kcal
            """,
            food_id,
        )
    return {
        "portions": [
            {
                "name": r["name"],
                "grams": float(r["grams"]) if r["grams"] is not None else None,
                "kcal": float(r["kcal"]),
                "protein_g": float(r["protein_g"]),
                "carbs_g": float(r["carbs_g"]),
                "fat_g": float(r["fat_g"]),
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------
# 5. estimate_meal_from_photo (Vision LLM + matcher + calc)
# --------------------------------------------------------------
@tool(
    name="estimate_meal_from_photo",
    description=(
        "Analisa foto de um PRATO de comida: identifica items, busca no banco e calcula "
        "macros reais. Use photo_id da última foto enviada pelo usuário. "
        "Retorna breakdown completo. NÃO loga ainda — espera confirmação do user."
    ),
    parameters={
        "type": "object",
        "properties": {
            "photo_id": {"type": "string", "description": "OPCIONAL. Se omitido, usa a última foto da conversa. Prefira omitir — é mais confiável que copiar o ID."},
            "context": {"type": "string", "description": "Contexto opcional: 'é a Picanha do cardápio anterior'"},
        },
    },
)
async def estimate_meal_from_photo(ctx: dict, photo_id: str | None = None, context: str | None = None) -> dict:
    image_bytes = await _download_photo(ctx, photo_id)
    if not image_bytes:
        return {"error": "Não consegui baixar a foto"}

    try:
        analysis, _raw = await llm.identify_items(
            image_bytes, "image/jpeg", ctx["vision_model"]
        )
    except Exception as e:
        return {"error": f"Vision LLM falhou: {e}"}

    llm_items = analysis.get("items", [])
    if not llm_items:
        return {"items": [], "totals": _zero_totals(), "note": "não identifiquei comida na foto"}

    resolved, totals = await calculator.calc_meal(llm_items)
    return {
        "items": [
            {
                "name_llm": it["name_llm"],
                "matched_to": it.get("food_name"),
                "portion_g": it["portion_g"],
                "kcal": it["kcal"],
                "protein_g": it["protein_g"],
                "source": it["source"],
            }
            for it in resolved
        ],
        "totals": totals,
        "_resolved": resolved,  # campo interno pra log_meal usar depois
        "notes": analysis.get("notes"),
    }


# --------------------------------------------------------------
# 6. parse_menu (Vision LLM com prompt de cardápio)
# --------------------------------------------------------------
@tool(
    name="parse_menu",
    description="Lê foto de CARDÁPIO de restaurante. Extrai pratos com descrição, preço, categoria.",
    parameters={
        "type": "object",
        "properties": {
            "photo_id": {"type": "string", "description": "OPCIONAL. Se omitido, usa a última foto."},
        },
    },
)
async def parse_menu(ctx: dict, photo_id: str | None = None) -> dict:
    image_bytes = await _download_photo(ctx, photo_id)
    if not image_bytes:
        return {"error": "Não consegui baixar a foto"}

    try:
        result = await llm.parse_menu_image(
            image_bytes, "image/jpeg", ctx["vision_model"]
        )
    except Exception as e:
        return {"error": f"Vision LLM falhou: {e}"}
    return result


# --------------------------------------------------------------
# 7. log_meal
# --------------------------------------------------------------
@tool(
    name="log_meal",
    description=(
        "Salva uma refeição no diário do usuário. Use SOMENTE após o user "
        "confirmar os items e porções. Geralmente vem após estimate_meal_from_photo."
    ),
    parameters={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Lista de itens. Cada um: name (str), portion_g (num), kcal (num), protein_g, carbs_g, fat_g, source (str), food_id (int opcional)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "portion_g": {"type": "number"},
                        "kcal": {"type": "number"},
                        "protein_g": {"type": "number"},
                        "carbs_g": {"type": "number"},
                        "fat_g": {"type": "number"},
                        "source": {"type": "string"},
                        "food_id": {"type": "integer"},
                    },
                    "required": ["name", "portion_g", "kcal", "protein_g", "carbs_g", "fat_g"],
                },
            },
            "notes": {"type": "string"},
        },
        "required": ["items"],
    },
)
async def log_meal(ctx: dict, items: list, notes: str | None = None) -> dict:
    totals = {
        "kcal": round(sum(float(i["kcal"]) for i in items), 1),
        "protein_g": round(sum(float(i["protein_g"]) for i in items), 1),
        "carbs_g": round(sum(float(i["carbs_g"]) for i in items), 1),
        "fat_g": round(sum(float(i["fat_g"]) for i in items), 1),
    }
    # normaliza items pro schema interno
    norm_items = []
    for it in items:
        norm_items.append({
            "name_llm": it["name"],
            "food_id": it.get("food_id"),
            "food_name": it.get("food_name"),
            "portion_g": float(it["portion_g"]),
            "kcal": float(it["kcal"]),
            "protein_g": float(it["protein_g"]),
            "carbs_g": float(it["carbs_g"]),
            "fat_g": float(it["fat_g"]),
            "source": it.get("source", "agente"),
        })
    meal_id = await db.insert_meal(
        user_id=ctx["user_id"],
        vision_model=ctx.get("vision_model", "agent"),
        photo_file_id=None,
        items=norm_items,
        totals=totals,
        notes=notes,
    )
    # Auto-anexa gráfico do dia pra acompanhar a confirmação
    await _attach_daily_chart_to_scratchpad(ctx)
    return {"meal_id": meal_id, "totals": totals, "daily_chart_attached": True}


# --------------------------------------------------------------
# 8. get_today_summary
# --------------------------------------------------------------
@tool(
    name="get_today_summary",
    description="Totais e refeições do dia atual do usuário.",
    parameters={"type": "object", "properties": {}},
)
async def get_today_summary(ctx: dict) -> dict:
    meals = await db.list_today(ctx["user_id"])
    if not meals:
        return {"meals": [], "totals": _zero_totals()}
    totals = {
        "kcal": round(sum(float(m["kcal"]) for m in meals), 1),
        "protein_g": round(sum(float(m["protein_g"]) for m in meals), 1),
        "carbs_g": round(sum(float(m["carbs_g"]) for m in meals), 1),
        "fat_g": round(sum(float(m["fat_g"]) for m in meals), 1),
    }
    return {
        "meals": [
            {
                "meal_id": m["id"],
                "time": m["eaten_at"].strftime("%H:%M"),
                "kcal": float(m["kcal"]),
                "items_short": [
                    (i.get("food_name") or i["name_llm"])
                    for i in (m["items"] if isinstance(m["items"], list) else json.loads(m["items"]))
                ][:4],
            }
            for m in meals
        ],
        "totals": totals,
    }


# --------------------------------------------------------------
# 9. get_recent_meals
# --------------------------------------------------------------
@tool(
    name="get_recent_meals",
    description="Refeições dos últimos N dias (default 7). Use pra perguntas tipo 'comi a mesma coisa de ontem'.",
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "default": 7},
        },
    },
)
async def get_recent_meals(ctx: dict, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pool = db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, eaten_at, items, kcal, protein_g
            from meals where user_id=$1 and eaten_at>=$2
            order by eaten_at desc limit 50
            """,
            ctx["user_id"], since,
        )
    return {
        "meals": [
            {
                "meal_id": r["id"],
                "date": r["eaten_at"].strftime("%Y-%m-%d %H:%M"),
                "kcal": float(r["kcal"]),
                "protein_g": float(r["protein_g"]),
                "items": [
                    (i.get("food_name") or i["name_llm"])
                    for i in (r["items"] if isinstance(r["items"], list) else json.loads(r["items"]))
                ],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------
# 10. get_user_profile
# --------------------------------------------------------------
# Onboarding em 3 fases. Fases 1+2 são obrigatórias (6 perguntas) — depois disso
# já dá pra calcular meta provisória com defaults sensatos e o user pode usar tudo.
# Fase 3 é opcional, refina os 3 últimos campos.
ONBOARDING_PHASE_1 = ["timezone", "sex", "birth_date"]
ONBOARDING_PHASE_2 = ["height_cm", "current_weight_kg", "target_weight_kg"]
ONBOARDING_PHASE_3 = ["activity_level", "weekly_rate_kg", "eatback_pct"]

_ONBOARDING_ORDER = ONBOARDING_PHASE_1 + ONBOARDING_PHASE_2 + ONBOARDING_PHASE_3
_REQUIRED_FIELDS = ONBOARDING_PHASE_1 + ONBOARDING_PHASE_2

# Defaults pra meta provisória (fase 2 completa, fase 3 pendente)
_PROVISIONAL_DEFAULTS = {
    "activity_level": "sedentary",
    "weekly_rate_kg": -0.5,
    "eatback_pct": 100,
}

_QUESTION_TEMPLATES = {
    "timezone": (
        "Onde você mora? Pode ser cidade/estado/país. "
        "Vou usar isso pra horários e lembretes ficarem certos.\n"
        "Ex: 'Rio de Janeiro', 'São Paulo', 'Acre', 'Manaus', 'Lisboa'."
    ),
    "sex": "Qual seu sexo biológico? Responda M (masculino), F (feminino) ou O (outro).",
    "birth_date": "Qual sua data de nascimento? Formato YYYY-MM-DD (ex: 1995-03-21).",
    "height_cm": "Qual sua altura em cm? (ex: 178)",
    "current_weight_kg": "Qual seu peso atual em kg? (ex: 75.5)",
    "target_weight_kg": "Qual seu peso-alvo em kg?",
    "activity_level": (
        "Qual seu nível de atividade da ROTINA (sem contar treinos)?\n"
        "  • sedentary: trabalho de mesa, pouquíssimo movimento\n"
        "  • light: anda um pouco no escritório/casa\n"
        "  • moderate: trabalho com circulação (professor, garçom)\n"
        "  • active: trabalho braçal\n"
        "  • very_active: trabalho fisicamente muito demandante\n"
        "Treino entra separado via log_exercise."
    ),
    "weekly_rate_kg": (
        "Que ritmo de mudança de peso por semana você quer? "
        "Use número negativo pra perder (ex: -0.5) ou positivo pra ganhar. "
        "Sugestão padrão: -0.5 kg/semana se BMI normal, -0.75 a -1.0 se BMI alto."
    ),
    "eatback_pct": (
        "Última: quando você queima calorias no exercício, quanto disso quer adicionar "
        "ao seu limite do dia? Responda um número de 0 a 100. "
        "100 = come tudo de volta (padrão MFP). 50 = come metade (Noom). 0 = ignora."
    ),
}


@tool(
    name="get_user_profile",
    description=(
        "Perfil completo do usuário. SEMPRE retorna 'missing' (campos pendentes) e "
        "'next_question' (a próxima pergunta exata a fazer no onboarding, se houver). "
        "Use ESSA pergunta literal — não invente outra. Se 'missing' está vazio, NÃO "
        "comece onboarding."
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_user_profile(ctx: dict) -> dict:
    p = await db.get_profile(ctx["user_id"]) or {}

    missing_all = [k for k in _ONBOARDING_ORDER if p.get(k) is None]
    missing_required = [k for k in _REQUIRED_FIELDS if p.get(k) is None]
    missing_phase_1 = [k for k in ONBOARDING_PHASE_1 if p.get(k) is None]
    missing_phase_2 = [k for k in ONBOARDING_PHASE_2 if p.get(k) is None]
    missing_phase_3 = [k for k in ONBOARDING_PHASE_3 if p.get(k) is None]

    next_field = missing_all[0] if missing_all else None
    next_question = _QUESTION_TEMPLATES.get(next_field) if next_field else None

    if missing_phase_1:
        current_phase = 1
    elif missing_phase_2:
        current_phase = 2
    elif missing_phase_3:
        current_phase = 3
    else:
        current_phase = None

    all_required_filled = not missing_required
    optional_pending = bool(missing_phase_3)

    base = {
        "exists": bool(p),
        "name": p.get("name"),
        "timezone": p.get("timezone") or "America/Sao_Paulo",
        "sex": p.get("sex"),
        "birth_date": str(p["birth_date"]) if p.get("birth_date") else None,
        "height_cm": p.get("height_cm"),
        "current_weight_kg": float(p["current_weight_kg"]) if p.get("current_weight_kg") else None,
        "target_weight_kg": float(p["target_weight_kg"]) if p.get("target_weight_kg") else None,
        "activity_level": p.get("activity_level"),
        "weekly_rate_kg": float(p["weekly_rate_kg"]) if p.get("weekly_rate_kg") is not None else None,
        "eatback_pct": p.get("eatback_pct"),
        "daily_kcal": p.get("daily_kcal"),
        "daily_protein_g": p.get("daily_protein_g"),
        "preferences": p.get("preferences"),
        "current_phase": current_phase,
        "all_required_filled": all_required_filled,
        "can_use_app": all_required_filled,
        "optional_pending": optional_pending,
        "missing": missing_all,
        "next_field": next_field,
        "next_question": next_question,
    }

    if missing_required:
        base["hint"] = (
            f"Onboarding em andamento (fase {current_phase}/3). Faça SÓ next_question. "
            "Salve com set_profile. NÃO invente nome."
        )
    elif optional_pending and not p.get("daily_kcal"):
        base["hint"] = (
            "🎉 Fase 2 completa! AGORA chame compute_daily_goal(provisional=true) "
            "pra calcular meta com defaults (sedentary, -0.5kg/sem, eatback 100%). "
            "Depois pergunte se o user quer afinar com 3 perguntas extras (fase 3)."
        )
    elif optional_pending:
        base["hint"] = (
            "Meta provisória calculada. Se user pedir 'afinar/refinar/ajustar', inicie fase 3 "
            "(atividade, ritmo, eatback). Senão, responda normalmente o pedido dele."
        )
    else:
        base["hint"] = "Perfil completo. NÃO faça onboarding — responda o pedido."

    return base


# --------------------------------------------------------------
# Perfil: set, compute, log_weight, compare
# --------------------------------------------------------------
_PROFILE_FIELDS = {
    "name": str,
    "sex": str,                  # 'M' | 'F' | 'O'
    "birth_date": str,           # 'YYYY-MM-DD'
    "height_cm": int,
    "current_weight_kg": float,
    "target_weight_kg": float,
    "activity_level": str,       # 'sedentary'|'light'|'moderate'|'active'|'very_active'
    "weekly_rate_kg": float,
    "eatback_pct": int,
    "preferences": str,
}


@tool(
    name="set_profile",
    description=(
        "Atualiza UM campo do perfil do usuário. Use a cada resposta durante onboarding. "
        "Campos válidos: name, sex (M/F/O), birth_date (YYYY-MM-DD), height_cm, "
        "current_weight_kg, target_weight_kg, "
        "activity_level (sedentary/light/moderate/active/very_active), "
        "weekly_rate_kg (negativo=perder, ex: -0.5), eatback_pct (0-100), preferences (texto)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "field": {"type": "string"},
            "value": {"type": "string", "description": "Valor como string; será convertido"},
        },
        "required": ["field", "value"],
    },
)
async def set_profile(ctx: dict, field: str, value: str) -> dict:
    if field not in _PROFILE_FIELDS:
        return {"error": f"Campo inválido: {field}", "valid_fields": list(_PROFILE_FIELDS)}
    try:
        typed = _PROFILE_FIELDS[field](value) if _PROFILE_FIELDS[field] is not str else value
    except (TypeError, ValueError):
        return {"error": f"Valor '{value}' não pode ser convertido pra {_PROFILE_FIELDS[field].__name__}"}

    await db.upsert_profile_field(ctx["user_id"], field, typed)
    return {"ok": True, "field": field, "value": typed}


@tool(
    name="compute_daily_goal",
    description=(
        "Calcula meta calórica diária (Mifflin-St Jeor) + sugestão de macros. "
        "Salva no perfil. Use provisional=true APÓS fase 2 do onboarding (sem ter "
        "atividade/ritmo/eatback ainda) — preenche defaults sedentary/-0.5/100%. "
        "Use provisional=false (default) quando o user tiver completado a fase 3."
    ),
    parameters={
        "type": "object",
        "properties": {
            "provisional": {"type": "boolean", "default": False},
        },
    },
)
async def compute_daily_goal(ctx: dict, provisional: bool = False) -> dict:
    p = await db.get_profile(ctx["user_id"])
    if not p:
        return {"error": "Perfil não existe — faça set_profile primeiro"}

    # Se for provisória e faltam campos da fase 3, injeta defaults
    if provisional:
        for field, default in _PROVISIONAL_DEFAULTS.items():
            if p.get(field) is None:
                await db.upsert_profile_field(ctx["user_id"], field, default)
                p[field] = default

    kcal = goals.daily_goal_kcal(p)
    if kcal is None:
        return {"error": "Faltam campos no perfil pra calcular",
                "missing": [k for k in ("sex", "birth_date", "height_cm", "current_weight_kg",
                                         "activity_level", "weekly_rate_kg")
                            if p.get(k) is None]}
    protein = goals.suggested_protein_g(p)
    macros = goals.macros_split(kcal, protein)
    await db.upsert_profile_field(ctx["user_id"], "daily_kcal", kcal)
    await db.upsert_profile_field(ctx["user_id"], "daily_protein_g", protein)
    return {
        "daily_kcal": kcal,
        "protein_g": macros["protein_g"],
        "carbs_g": macros["carbs_g"],
        "fat_g": macros["fat_g"],
        "is_provisional": provisional,
        "explanation": (
            "Mifflin-St Jeor + fator de atividade ± déficit/superávit (7700 kcal/kg)."
            + (" Defaults: sedentary, -0.5 kg/sem, eatback 100%." if provisional else "")
        ),
    }


@tool(
    name="log_weight",
    description=(
        "Registra peso atual + atualiza current_weight_kg + RECALCULA a meta diária "
        "automaticamente (Mifflin novamente com o peso novo). Retorna a nova meta."
    ),
    parameters={
        "type": "object",
        "properties": {
            "weight_kg": {"type": "number"},
            "source": {"type": "string", "description": "'manual' | 'photo' (default manual)"},
        },
        "required": ["weight_kg"],
    },
)
async def log_weight(ctx: dict, weight_kg: float, source: str = "manual") -> dict:
    await db.insert_weight_log(ctx["user_id"], weight_kg, source=source)
    await db.upsert_profile_field(ctx["user_id"], "current_weight_kg", float(weight_kg))

    # Auto-recalcula meta
    p = await db.get_profile(ctx["user_id"])
    new_goal_msg = None
    if p and goals.daily_goal_kcal(p) is not None:
        new_kcal = goals.daily_goal_kcal(p)
        new_protein = goals.suggested_protein_g(p)
        await db.upsert_profile_field(ctx["user_id"], "daily_kcal", new_kcal)
        await db.upsert_profile_field(ctx["user_id"], "daily_protein_g", new_protein)
        old_kcal = p.get("daily_kcal")
        delta = (new_kcal - old_kcal) if old_kcal else 0
        new_goal_msg = {
            "new_daily_kcal": new_kcal,
            "old_daily_kcal": old_kcal,
            "delta_kcal": delta,
            "new_protein_g": new_protein,
        }
    return {
        "ok": True,
        "weight_kg": weight_kg,
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "goal_update": new_goal_msg,
    }


@tool(
    name="parse_scale_photo",
    description=(
        "Lê foto de uma balança (digital ou analógica) e extrai o peso em kg. "
        "NÃO loga — apresente ao usuário pra confirmar, depois chame log_weight."
    ),
    parameters={
        "type": "object",
        "properties": {"photo_id": {"type": "string", "description": "OPCIONAL. Se omitido, usa a última foto."}},
    },
)
async def parse_scale_photo(ctx: dict, photo_id: str | None = None) -> dict:
    image_bytes = await _download_photo(ctx, photo_id)
    if not image_bytes:
        return {"error": "Não consegui baixar a foto"}
    try:
        data = await llm.parse_scale_image(image_bytes, "image/jpeg", ctx["vision_model"])
    except Exception as e:
        return {"error": f"Vision LLM falhou: {e}"}
    data["_photo_id"] = photo_id
    return data


@tool(
    name="compare_to_goal",
    description=(
        "Compara intake e exercício do dia com a meta. Aplica eatback_pct. "
        "Retorna: consumido, queimado, restante, status (déficit/superávit)."
    ),
    parameters={"type": "object", "properties": {}},
)
async def compare_to_goal(ctx: dict) -> dict:
    p = await db.get_profile(ctx["user_id"])
    if not p or not p.get("daily_kcal"):
        return {"error": "Sem meta diária. Use compute_daily_goal primeiro."}
    intake = await db.sum_today_meals(ctx["user_id"])
    burned = await db.sum_today_exercises(ctx["user_id"])
    eatback_pct = int(p.get("eatback_pct", 100))
    bonus_from_exercise = round(burned * eatback_pct / 100)
    adjusted_budget = p["daily_kcal"] + bonus_from_exercise
    remaining = adjusted_budget - intake["kcal"]
    return {
        "goal_kcal": p["daily_kcal"],
        "intake_kcal": round(intake["kcal"], 0),
        "intake_protein_g": round(intake["protein_g"], 0),
        "burned_kcal": burned,
        "eatback_pct": eatback_pct,
        "bonus_from_exercise": bonus_from_exercise,
        "adjusted_budget": adjusted_budget,
        "remaining_kcal": round(remaining, 0),
        "status": "déficit" if remaining > 0 else ("equilibrado" if abs(remaining) < 50 else "estouro"),
    }


# --------------------------------------------------------------
# Exercícios: parse_watch_photo, log_exercise, get_*, get_balance
# --------------------------------------------------------------
@tool(
    name="parse_watch_photo",
    description=(
        "Lê foto/screenshot de relógio fitness (Apple Watch, Garmin, Strava, etc) e "
        "extrai dados do treino. NÃO loga sozinho — apresente ao usuário e peça confirmação."
    ),
    parameters={
        "type": "object",
        "properties": {
            "photo_id": {"type": "string", "description": "OPCIONAL. Se omitido, usa a última foto."},
        },
    },
)
async def parse_watch_photo(ctx: dict, photo_id: str | None = None) -> dict:
    image_bytes = await _download_photo(ctx, photo_id)
    if not image_bytes:
        return {"error": "Não consegui baixar a foto"}
    try:
        data = await llm.parse_watch_image(image_bytes, "image/jpeg", ctx["vision_model"])
    except Exception as e:
        return {"error": f"Vision LLM falhou: {e}"}
    data["_photo_id"] = photo_id  # pra log_exercise usar
    return data


@tool(
    name="log_exercise",
    description="Registra um exercício no diário. Use após confirmação do usuário.",
    parameters={
        "type": "object",
        "properties": {
            "activity": {"type": "string", "description": "ex: 'corrida', 'musculação'"},
            "kcal_burned": {"type": "integer"},
            "duration_min": {"type": "integer"},
            "distance_km": {"type": "number"},
            "avg_hr": {"type": "integer"},
            "done_at": {"type": "string", "description": "ISO 8601, opcional"},
            "photo_file_id": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["activity", "kcal_burned"],
    },
)
async def log_exercise(ctx: dict, activity: str, kcal_burned: int,
                       duration_min: int | None = None,
                       distance_km: float | None = None,
                       avg_hr: int | None = None,
                       done_at: str | None = None,
                       photo_file_id: str | None = None,
                       notes: str | None = None) -> dict:
    done = None
    if done_at:
        try:
            done = datetime.fromisoformat(done_at.replace("Z", "+00:00"))
        except ValueError:
            done = None
    ex_id = await db.insert_exercise(
        user_id=ctx["user_id"],
        activity=activity,
        kcal_burned=int(kcal_burned),
        duration_min=duration_min,
        distance_km=distance_km,
        avg_hr=avg_hr,
        done_at=done,
        source="agent" if not photo_file_id else "watch_photo",
        photo_file_id=photo_file_id,
        notes=notes,
    )
    return {"exercise_id": ex_id, "activity": activity, "kcal_burned": kcal_burned}


@tool(
    name="get_exercises_today",
    description="Exercícios registrados hoje + total de kcal queimado.",
    parameters={"type": "object", "properties": {}},
)
async def get_exercises_today(ctx: dict) -> dict:
    rows = await db.list_exercises_today(ctx["user_id"])
    total = sum(r["kcal_burned"] for r in rows)
    return {
        "exercises": [
            {
                "id": r["id"],
                "activity": r["activity"],
                "kcal": r["kcal_burned"],
                "duration_min": r.get("duration_min"),
                "time": r["done_at"].strftime("%H:%M"),
            }
            for r in rows
        ],
        "total_burned_kcal": total,
    }


@tool(
    name="get_calorie_balance",
    description=(
        "Mesma coisa que compare_to_goal mas com mais detalhe — útil pra mostrar "
        "ao usuário 'você comeu X, queimou Y, ainda pode comer Z'."
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_calorie_balance(ctx: dict) -> dict:
    return await compare_to_goal(ctx)


# --------------------------------------------------------------
# Relatórios temporais
# --------------------------------------------------------------
@tool(
    name="get_period_summary",
    description=(
        "Resumo de intake + exercício + balance por dia, ao longo dos últimos N dias. "
        "Use pra responder 'como foi minha semana?' ou 'média de calorias dos últimos 30 dias?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "default": 7, "description": "1-90"},
        },
    },
)
async def get_period_summary(ctx: dict, days: int = 7) -> dict:
    days = max(1, min(90, days))
    data = await db.period_summary(ctx["user_id"], days)
    p = await db.get_profile(ctx["user_id"])
    goal = p.get("daily_kcal") if p else None
    return {
        "days": days,
        "goal_kcal": goal,
        "per_day": data["per_day"],
        "totals": data["totals"],
        "averages": data["averages"],
    }


async def _attach_daily_chart_to_scratchpad(ctx: dict) -> None:
    """Helper: gera gráfico do dia e empilha no scratchpad pra bot enviar."""
    try:
        png = await _build_daily_chart(ctx["user_id"])
    except Exception:
        log.exception("falha gerando gráfico diário")
        return
    import base64
    b64 = base64.b64encode(png).decode("ascii")
    pool = db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update conversations
            set scratchpad = scratchpad || jsonb_build_object('_pending_chart_b64', $1::text)
            where id = $2
            """,
            b64, ctx["conv_id"],
        )


async def _build_daily_chart(user_id: int) -> bytes:
    """Monta os dados e chama o renderer de gráfico diário."""
    intake = await db.sum_today_meals(user_id)
    burned = await db.sum_today_exercises(user_id)
    p = await db.get_profile(user_id) or {}
    meals = await db.list_today(user_id)
    meals_view = []
    for m in meals:
        items = m["items"] if isinstance(m["items"], list) else json.loads(m["items"])
        label = ", ".join(
            (i.get("food_name") or i.get("name_llm") or "?")[:18] for i in items[:2]
        )
        meals_view.append({
            "time": m["eaten_at"].strftime("%H:%M"),
            "label": label or "refeição",
            "kcal": float(m["kcal"]),
        })
    return charts.daily_progress(
        intake_kcal=intake["kcal"],
        intake_protein_g=intake["protein_g"],
        intake_carbs_g=intake["carbs_g"],
        intake_fat_g=intake["fat_g"],
        burned_kcal=burned,
        goal_kcal=p.get("daily_kcal"),
        goal_protein_g=p.get("daily_protein_g"),
        eatback_pct=int(p.get("eatback_pct") or 100),
        meals=meals_view,
        title=f"Hoje — {datetime.now(timezone.utc).strftime('%d/%m')}",
    )


@tool(
    name="generate_daily_chart",
    description=(
        "Gera gráfico bonito do dia (anel de kcal vs meta + barras de macros + "
        "refeições). Anexa pro bot enviar. Use quando o usuário pedir 'gráfico do dia', "
        "'como tá hoje visualmente', etc."
    ),
    parameters={"type": "object", "properties": {}},
)
async def generate_daily_chart(ctx: dict) -> dict:
    await _attach_daily_chart_to_scratchpad(ctx)
    return {"ok": True, "chart_attached": True}


@tool(
    name="generate_weight_chart",
    description=(
        "Gera gráfico PNG do histórico de peso (pontos diários + média móvel 7d + linha da meta). "
        "Use quando o usuário pedir 'evolução do peso' ou 'como tá indo'."
    ),
    parameters={
        "type": "object",
        "properties": {"days": {"type": "integer", "default": 60}},
    },
)
async def generate_weight_chart(ctx: dict, days: int = 60) -> dict:
    days = max(7, min(365, days))
    logs = await db.list_weight_logs(ctx["user_id"], days)
    p = await db.get_profile(ctx["user_id"])
    target = float(p["target_weight_kg"]) if p and p.get("target_weight_kg") else None
    if not logs:
        return {"error": "Sem pesagens registradas ainda. Use log_weight pra começar."}
    png_bytes = charts.weight_trend(logs=logs, target_kg=target, title=f"Peso — últimos {days} dias")
    import base64
    b64 = base64.b64encode(png_bytes).decode("ascii")
    pool = db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update conversations
            set scratchpad = scratchpad || jsonb_build_object('_pending_chart_b64', $1::text)
            where id = $2
            """,
            b64, ctx["conv_id"],
        )
    n = len(logs)
    first, last = logs[0]["weight_kg"], logs[-1]["weight_kg"]
    return {"ok": True, "chart_attached": True,
            "summary": f"{n} pesagens no período. {first:.1f} kg → {last:.1f} kg (Δ {last-first:+.1f} kg)."}


@tool(
    name="generate_report_chart",
    description=(
        "Gera gráfico PNG (intake vs queimado vs meta por dia) e retorna como file_id "
        "que o bot vai mostrar pro usuário. Use após get_period_summary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "default": 7},
            "title": {"type": "string"},
        },
    },
)
async def generate_report_chart(ctx: dict, days: int = 7, title: str | None = None) -> dict:
    days = max(1, min(90, days))
    data = await db.period_summary(ctx["user_id"], days)
    p = await db.get_profile(ctx["user_id"])
    goal = p.get("daily_kcal") if p else None
    png_bytes = charts.daily_intake_vs_goal(
        per_day=data["per_day"], goal_kcal=goal, title=title or f"Últimos {days} dias"
    )
    # Bot envia ao final do turno. Armazena no scratchpad pra runtime pegar.
    pool = db.pool()
    import base64
    b64 = base64.b64encode(png_bytes).decode("ascii")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update conversations
            set scratchpad = scratchpad || jsonb_build_object('_pending_chart_b64', $1::text)
            where id = $2
            """,
            b64, ctx["conv_id"],
        )
    return {"ok": True, "chart_attached": True,
            "summary_text": f"Gráfico de {days} dias gerado. Total intake: {data['totals']['intake_kcal']:.0f} kcal, queimado: {data['totals']['burned_kcal']}."}


# --------------------------------------------------------------
# get_bot_capabilities: lista comandos diretos + recursos do bot
# --------------------------------------------------------------
@tool(
    name="get_bot_capabilities",
    description=(
        "Lista TODOS os comandos diretos do bot (/comandos) e os recursos automáticos "
        "(scheduler de lembrete, auto-recálculo, etc). Use quando o user perguntar "
        "'tem comando X?', 'como faço Y?', 'consegue agendar Z?' ou similar. "
        "Você tem um resumo no system prompt — só chame esta tool se precisar de mais detalhe."
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_bot_capabilities_tool(ctx: dict) -> dict:
    from agent.prompts import BOT_CAPABILITIES
    return {
        "capabilities": BOT_CAPABILITIES,
        "note": "Use estas informações pra responder ao usuário com precisão."
    }


# --------------------------------------------------------------
# save_food_alias: ensina o matcher quando user aceita um substituto
# --------------------------------------------------------------
@tool(
    name="save_food_alias",
    description=(
        "Cria um alias permanente: quando 'unknown_name' aparecer no futuro, "
        "use direto o food_id (sem buscar). Use quando o usuário aceitou um "
        "SUBSTITUTO pra um alimento que nenhuma fonte tem. Ex: bolo de carne → "
        "carne moída cozida. Da próxima vez, search_foods('bolo de carne') retorna direto."
    ),
    parameters={
        "type": "object",
        "properties": {
            "unknown_name": {"type": "string", "description": "Nome que o usuário usou. Ex: 'bolo de carne'"},
            "food_id": {"type": "integer", "description": "ID do alimento na tabela foods"},
        },
        "required": ["unknown_name", "food_id"],
    },
)
async def save_food_alias_tool(ctx: dict, unknown_name: str, food_id: int) -> dict:
    food = await db.get_food(food_id)
    if not food:
        return {"error": f"food_id {food_id} não existe"}
    await matcher.save_alias(unknown_name, food_id, user_id=ctx["user_id"])
    return {
        "ok": True,
        "alias_saved": unknown_name,
        "mapped_to": food["name"],
        "note": "Próxima busca por '{}' vai retornar este alimento direto.".format(unknown_name),
    }


# --------------------------------------------------------------
# 11. remember (scratchpad da conversa atual)
# --------------------------------------------------------------
@tool(
    name="remember",
    description=(
        "Salva contexto leve na conversa atual (apenas pra próximos turnos da MESMA conversa). "
        "Ex: depois que user escolhe 'Picanha' do cardápio, remember('dish_chosen', 'Picanha'). "
        "Sobrescreve se chave já existir."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["key", "value"],
    },
)
async def remember(ctx: dict, key: str, value: str) -> dict:
    pool = db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update conversations
            set scratchpad = scratchpad || jsonb_build_object($1::text, $2::text)
            where id = $3
            """,
            key, value, ctx["conv_id"],
        )
    return {"ok": True, "remembered": {key: value}}


# --------------------------------------------------------------
# Helpers
# --------------------------------------------------------------
async def _download_photo(ctx: dict, photo_id: str) -> bytes | None:
    """Baixa foto via callable injetado pelo adapter (Telegram/WhatsApp).
    Se photo_id passado pelo LLM falhar (modelo corrompe IDs longos),
    tenta o latest_photo_id que o runtime injetou no ctx."""
    downloader = ctx.get("download_photo")
    if not downloader:
        return None

    async def _try(pid: str) -> bytes | None:
        try:
            return await downloader(pid)
        except Exception as e:
            log.warning("download_photo falhou pra %s: %s", pid[:30] + "...", e)
            return None

    # 1ª tentativa: o que o LLM passou
    if photo_id:
        data = await _try(photo_id)
        if data:
            return data

    # Fallback: a foto mais recente da conversa (que sabemos ser válida)
    fallback = ctx.get("latest_photo_id")
    if fallback and fallback != photo_id:
        log.info("usando latest_photo_id como fallback (LLM corrompeu o ID?)")
        data = await _try(fallback)
        if data:
            return data
    return None


def _zero_totals() -> dict:
    return {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}
