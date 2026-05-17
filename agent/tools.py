"""As 11 tools que o agente pode chamar.

Cada tool retorna dict serializável (JSON). ctx é passado pelo runtime e contém:
  ctx['user_id']       : int
  ctx['conv_id']       : int  (id da conversa atual)
  ctx['bot']           : objeto telegram.Bot pra baixar fotos
  ctx['vision_model']  : slug do modelo de visão
"""
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import calculator
import db
import llm
import matcher
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
        return {"hits": [{"vitat_id": h.id, "name": h.name} for h in hits]}
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
            "photo_id": {"type": "string", "description": "file_id Telegram da foto, vem como [photo: xxx] na msg do user"},
            "context": {"type": "string", "description": "Contexto opcional: 'é a Picanha do cardápio anterior'"},
        },
        "required": ["photo_id"],
    },
)
async def estimate_meal_from_photo(ctx: dict, photo_id: str, context: str | None = None) -> dict:
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
            "photo_id": {"type": "string"},
        },
        "required": ["photo_id"],
    },
)
async def parse_menu(ctx: dict, photo_id: str) -> dict:
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
    return {"meal_id": meal_id, "totals": totals}


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
@tool(
    name="get_user_profile",
    description="Perfil do usuário: metas calóricas, preferências, restrições.",
    parameters={"type": "object", "properties": {}},
)
async def get_user_profile(ctx: dict) -> dict:
    pool = db.pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from user_profiles where user_id=$1", ctx["user_id"]
        )
    if not row:
        return {
            "exists": False,
            "hint": "Usuário não definiu perfil. Pode perguntar metas e preferências e salvar com set_user_profile."
        }
    return {
        "exists": True,
        "name": row["name"],
        "daily_kcal": row["daily_kcal"],
        "daily_protein_g": row["daily_protein_g"],
        "preferences": row["preferences"],
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
    bot = ctx.get("bot")
    if not bot:
        return None
    try:
        f = await bot.get_file(photo_id)
        buf = io.BytesIO()
        await f.download_to_memory(out=buf)
        return buf.getvalue()
    except Exception:
        log.exception("falha baixando foto %s", photo_id)
        return None


def _zero_totals() -> dict:
    return {"kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}
