import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Supabase coloca extensions (unaccent, pg_trgm) no schema "extensions".
    # Estendemos search_path pra usar os operadores/funções sem qualificar tudo.
    await conn.execute("set search_path to public, extensions")


async def init() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"],
        min_size=1,
        max_size=5,
        init=_init_conn,
        statement_cache_size=0,  # Supabase Transaction pooler (pgbouncer) não aceita prepared stmts
    )


async def close() -> None:
    if _pool is not None:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.init() não foi chamado")
    return _pool


async def insert_meal(
    user_id: int,
    vision_model: str,
    photo_file_id: str | None,
    items: list[dict],
    totals: dict,
    notes: str | None,
) -> int:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into meals (user_id, vision_model, photo_file_id, items,
                               kcal, protein_g, carbs_g, fat_g, notes)
            values ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9)
            returning id
            """,
            user_id,
            vision_model,
            photo_file_id,
            json.dumps(items),
            totals["kcal"],
            totals["protein_g"],
            totals["carbs_g"],
            totals["fat_g"],
            notes,
        )
    return row["id"]


async def get_meal(meal_id: int, user_id: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "select * from meals where id=$1 and user_id=$2", meal_id, user_id
        )
    return dict(row) if row else None


async def update_meal_items(meal_id: int, items: list[dict], totals: dict) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            update meals
            set items=$1::jsonb, kcal=$2, protein_g=$3, carbs_g=$4, fat_g=$5
            where id=$6
            """,
            json.dumps(items),
            totals["kcal"],
            totals["protein_g"],
            totals["carbs_g"],
            totals["fat_g"],
            meal_id,
        )


async def delete_meal(user_id: int, meal_id: int) -> bool:
    async with pool().acquire() as conn:
        r = await conn.execute("delete from meals where id=$1 and user_id=$2", meal_id, user_id)
    return r.endswith(" 1")


async def delete_last(user_id: int) -> int | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            delete from meals
            where id = (select id from meals where user_id=$1 order by created_at desc limit 1)
            returning id
            """,
            user_id,
        )
    return row["id"] if row else None


async def summary_since(user_id: int, since: datetime) -> dict:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            select count(*) as n,
                   coalesce(sum(kcal),0) as kcal,
                   coalesce(sum(protein_g),0) as protein_g,
                   coalesce(sum(carbs_g),0) as carbs_g,
                   coalesce(sum(fat_g),0) as fat_g
            from meals where user_id=$1 and eaten_at>=$2
            """,
            user_id,
            since,
        )
    return dict(row)


async def list_today(user_id: int) -> list[dict]:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = start - timedelta(hours=3)  # aproxima BRT
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            select id, items, kcal, protein_g, carbs_g, fat_g, eaten_at
            from meals
            where user_id=$1 and eaten_at>=$2
            order by eaten_at
            """,
            user_id,
            start,
        )
    return [dict(r) for r in rows]


async def get_food(food_id: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow("select * from foods where id=$1", food_id)
    return dict(row) if row else None


async def count_foods() -> int:
    async with pool().acquire() as conn:
        return await conn.fetchval("select count(*) from foods")


async def close_active_conversation(user_id: int) -> None:
    """Marca conversa ativa do usuário como closed. Próxima msg cria nova."""
    async with pool().acquire() as conn:
        await conn.execute(
            "update conversations set state='closed' where user_id=$1 and state='active'",
            user_id,
        )
