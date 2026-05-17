"""Carrega data/taco.json no Postgres. Idempotente via (source, source_id) unique.

Uso:
  python import_taco.py
"""
import asyncio
import json
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from matcher import normalize

load_dotenv()

TACO_PATH = Path(__file__).parent / "data" / "taco.json"


def _num(v) -> float | None:
    """TACO usa 'NA', 'Tr' (trace), '' como sentinelas. Tr -> 0, resto -> None."""
    if v is None or v == "" or v == "NA":
        return None
    if v == "Tr":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def main() -> None:
    raw = json.loads(TACO_PATH.read_text(encoding="utf-8"))
    rows = []
    skipped = 0
    for item in raw:
        kcal = _num(item.get("energy_kcal"))
        protein = _num(item.get("protein_g"))
        carbs = _num(item.get("carbohydrate_g"))
        fat = _num(item.get("lipid_g"))
        if None in (kcal, protein, carbs, fat):
            skipped += 1
            continue
        name = item["description"].strip()
        rows.append((
            "TACO",
            int(item["id"]),
            name,
            normalize(name),
            item.get("category"),
            kcal,
            protein,
            carbs,
            fat,
            _num(item.get("fiber_g")),
        ))

    async def _init_conn(c):
        await c.execute("set search_path to public, extensions")

    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"],
        min_size=1,
        max_size=2,
        init=_init_conn,
        statement_cache_size=0,
    )
    async with pool.acquire() as conn:
        # Garante schema antes de inserir
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        await conn.execute(schema)

        await conn.executemany(
            """
            insert into foods (source, source_id, name, name_normalized, category, kcal, protein_g, carbs_g, fat_g, fiber_g)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            on conflict (source, source_id) do update set
                name = excluded.name,
                name_normalized = excluded.name_normalized,
                category = excluded.category,
                kcal = excluded.kcal,
                protein_g = excluded.protein_g,
                carbs_g = excluded.carbs_g,
                fat_g = excluded.fat_g,
                fiber_g = excluded.fiber_g
            """,
            rows,
        )
        n = await conn.fetchval("select count(*) from foods")
    await pool.close()
    print(f"OK. Inseridos/atualizados: {len(rows)}. Pulados (sem macros essenciais): {skipped}. Total no banco: {n}.")


if __name__ == "__main__":
    asyncio.run(main())
