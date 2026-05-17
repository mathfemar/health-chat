"""Combina items da Vision LLM com matches do banco e calcula macros finais."""
import db
import matcher


async def calc_meal(llm_items: list[dict]) -> tuple[list[dict], dict]:
    """Para cada item da LLM, faz match e calcula macros reais.

    Retorna (resolved_items, totals).
    resolved_items: [
      {name_llm, portion_g, food_id, food_name, kcal, protein_g, carbs_g, fat_g,
       source, match_score, match_method, alternatives}
    ]
    """
    resolved = []
    for item in llm_items:
        name = item["name"]
        portion = float(item.get("portion_g") or 0)
        is_processed = bool(item.get("is_processed"))

        if portion <= 0:
            continue

        # Processados (Coca, sorvete Magnum) usam estimativa da LLM direto, pulam matcher
        if is_processed and item.get("estimated_kcal_per_100g") is not None:
            kcal_100 = float(item["estimated_kcal_per_100g"])
            p_100 = float(item.get("estimated_protein_per_100g") or 0)
            c_100 = float(item.get("estimated_carbs_per_100g") or 0)
            f_100 = float(item.get("estimated_fat_per_100g") or 0)
            factor = portion / 100.0
            resolved.append({
                "name_llm": name,
                "portion_g": portion,
                "food_id": None,
                "food_name": None,
                "kcal": round(kcal_100 * factor, 1),
                "protein_g": round(p_100 * factor, 1),
                "carbs_g": round(c_100 * factor, 1),
                "fat_g": round(f_100 * factor, 1),
                "source": "estimativa",
                "match_score": 0.0,
                "match_method": "llm-estimate",
                "alternatives": [],
            })
            continue

        m = await matcher.match_one(name)
        if m.food_id is None:
            # Sem match no DB e não é processado — usa estimativa da LLM se ela deu
            if item.get("estimated_kcal_per_100g") is not None:
                kcal_100 = float(item["estimated_kcal_per_100g"])
                p_100 = float(item.get("estimated_protein_per_100g") or 0)
                c_100 = float(item.get("estimated_carbs_per_100g") or 0)
                f_100 = float(item.get("estimated_fat_per_100g") or 0)
                factor = portion / 100.0
                resolved.append({
                    "name_llm": name, "portion_g": portion,
                    "food_id": None, "food_name": None,
                    "kcal": round(kcal_100 * factor, 1),
                    "protein_g": round(p_100 * factor, 1),
                    "carbs_g": round(c_100 * factor, 1),
                    "fat_g": round(f_100 * factor, 1),
                    "source": "estimativa", "match_score": 0.0,
                    "match_method": "llm-estimate-fallback",
                    "alternatives": m.alternatives,
                })
            else:
                resolved.append({
                    "name_llm": name, "portion_g": portion,
                    "food_id": None, "food_name": None,
                    "kcal": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0,
                    "source": "sem dados", "match_score": 0.0,
                    "match_method": "none",
                    "alternatives": m.alternatives,
                })
            continue

        food = await db.get_food(m.food_id)
        factor = portion / 100.0
        resolved.append({
            "name_llm": name,
            "portion_g": portion,
            "food_id": food["id"],
            "food_name": food["name"],
            "kcal": round(float(food["kcal"]) * factor, 1),
            "protein_g": round(float(food["protein_g"]) * factor, 1),
            "carbs_g": round(float(food["carbs_g"]) * factor, 1),
            "fat_g": round(float(food["fat_g"]) * factor, 1),
            "source": "TACO",
            "match_score": m.score,
            "match_method": m.method,
            "alternatives": m.alternatives,
        })

    totals = {
        "kcal": round(sum(i["kcal"] for i in resolved), 1),
        "protein_g": round(sum(i["protein_g"] for i in resolved), 1),
        "carbs_g": round(sum(i["carbs_g"] for i in resolved), 1),
        "fat_g": round(sum(i["fat_g"] for i in resolved), 1),
    }
    return resolved, totals


async def recalc_with_override(items: list[dict], item_index: int, new_food_id: int) -> tuple[list[dict], dict]:
    """Troca o food de um item pelo escolhido pelo user e recalcula esse item."""
    items = [dict(i) for i in items]  # cópia rasa
    item = items[item_index]
    food = await db.get_food(new_food_id)
    if not food:
        return items, _totals(items)
    portion = float(item["portion_g"])
    factor = portion / 100.0
    item["food_id"] = food["id"]
    item["food_name"] = food["name"]
    item["kcal"] = round(float(food["kcal"]) * factor, 1)
    item["protein_g"] = round(float(food["protein_g"]) * factor, 1)
    item["carbs_g"] = round(float(food["carbs_g"]) * factor, 1)
    item["fat_g"] = round(float(food["fat_g"]) * factor, 1)
    item["source"] = "TACO"
    item["match_method"] = "user"
    item["match_score"] = 1.0
    return items, _totals(items)


def _totals(items: list[dict]) -> dict:
    return {
        "kcal": round(sum(float(i["kcal"]) for i in items), 1),
        "protein_g": round(sum(float(i["protein_g"]) for i in items), 1),
        "carbs_g": round(sum(float(i["carbs_g"]) for i in items), 1),
        "fat_g": round(sum(float(i["fat_g"]) for i in items), 1),
    }
