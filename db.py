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
    await _apply_lightweight_migrations()


async def _apply_lightweight_migrations() -> None:
    """ALTERs idempotentes pra evolução de schema sem destruir dados.
    Não substitui import_taco.py (que recria do zero) — só adiciona colunas novas."""
    migrations = [
        "alter table user_profiles add column if not exists weigh_in_minute int default 0",
        "alter table user_profiles add column if not exists timezone text default 'America/Sao_Paulo'",
        "update user_profiles set timezone='America/Sao_Paulo' where timezone is null",
        # Sprint 3: push proativo
        "alter table user_profiles add column if not exists push_enabled boolean default true",
        "alter table user_profiles add column if not exists push_lunch_hour int default 13",
        "alter table user_profiles add column if not exists push_lunch_minute int default 0",
        "alter table user_profiles add column if not exists push_dinner_hour int default 20",
        "alter table user_profiles add column if not exists push_dinner_minute int default 0",
        "alter table user_profiles add column if not exists push_lunch_last_date date",
        "alter table user_profiles add column if not exists push_dinner_last_date date",
        "alter table user_profiles add column if not exists push_friday_last_date date",
    ]
    async with _pool.acquire() as conn:
        for sql in migrations:
            try:
                await conn.execute(sql)
            except Exception as e:
                # tabela pode não existir ainda (ex: primeira execução)
                # nesse caso import_taco vai criar com o schema certo
                import logging
                logging.getLogger("db").warning("migration skipped: %s (%s)", sql, e)


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
    tz = await _user_tz(user_id)
    start = _day_start_utc(tz)
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


async def get_profile(user_id: int) -> dict | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow("select * from user_profiles where user_id=$1", user_id)
    return dict(row) if row else None


def _to_date(v):
    from datetime import date, datetime
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    # aceita "2003-03-15" ou "2003-03-15T00:00:00..."
    s = str(v)[:10]
    from datetime import date as _date
    return _date.fromisoformat(s)


_PROFILE_CONVERTERS = {
    "birth_date":        _to_date,
    "height_cm":         int,
    "eatback_pct":       int,
    "daily_kcal":        int,
    "daily_protein_g":   int,
    "current_weight_kg": float,
    "target_weight_kg":  float,
    "weekly_rate_kg":    float,
    "weigh_in_hour":     int,
    "weigh_in_minute":   int,
    "weigh_in_enabled":  lambda v: bool(v) if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes", "sim"),
    "push_enabled":      lambda v: bool(v) if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes", "sim"),
    "push_lunch_hour":   int,
    "push_lunch_minute": int,
    "push_dinner_hour":  int,
    "push_dinner_minute": int,
    # string fields ficam sem converter
}


async def upsert_profile_field(user_id: int, field: str, value) -> None:
    """Insere ou atualiza UM campo do perfil. Cria registro se não existe."""
    allowed = {
        "name", "sex", "birth_date", "height_cm", "current_weight_kg",
        "target_weight_kg", "activity_level", "weekly_rate_kg", "eatback_pct",
        "daily_kcal", "daily_protein_g", "preferences",
        "timezone",
        "weigh_in_enabled", "weigh_in_hour", "weigh_in_minute",
        "weigh_in_tz", "weigh_in_last_date",
        "push_enabled", "push_lunch_hour", "push_lunch_minute",
        "push_dinner_hour", "push_dinner_minute",
        "push_lunch_last_date", "push_dinner_last_date", "push_friday_last_date",
    }
    if field not in allowed:
        raise ValueError(f"Campo não permitido: {field}")

    conv = _PROFILE_CONVERTERS.get(field)
    if conv is not None:
        try:
            value = conv(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Valor inválido pra {field}: {value!r} ({e})")

    async with pool().acquire() as conn:
        await conn.execute(
            f"""
            insert into user_profiles (user_id, {field}, updated_at)
            values ($1, $2, now())
            on conflict (user_id) do update
            set {field} = excluded.{field}, updated_at = now()
            """,
            user_id, value,
        )


async def insert_weight_log(user_id: int, weight_kg: float, source: str = "manual") -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "insert into weight_log (user_id, weight_kg, source) values ($1, $2, $3)",
            user_id, weight_kg, source,
        )


async def list_weight_logs(user_id: int, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            select weight_kg, measured_at, source
            from weight_log where user_id=$1 and measured_at>=$2
            order by measured_at
            """,
            user_id, since,
        )
    return [{"weight_kg": float(r["weight_kg"]),
             "measured_at": r["measured_at"],
             "source": r["source"]} for r in rows]


async def users_due_for_weigh_in_reminder() -> list[dict]:
    """Retorna usuários que devem receber lembrete AGORA, dado o horário local deles.
    Filtra os que já receberam hoje (weigh_in_last_date == hoje local)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            select user_id, weigh_in_hour, weigh_in_minute,
                   coalesce(timezone, weigh_in_tz, 'America/Sao_Paulo') as timezone,
                   weigh_in_last_date
            from user_profiles
            where weigh_in_enabled = true and weigh_in_hour is not null
            """
        )
    return [dict(r) for r in rows]


async def mark_reminder_sent(user_id: int, local_date) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "update user_profiles set weigh_in_last_date=$1 where user_id=$2",
            local_date, user_id,
        )


async def users_due_for_push() -> list[dict]:
    """Retorna users com push habilitado. Filtra status no Python (timezone)."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            select user_id, timezone,
                   push_lunch_hour, push_lunch_minute,
                   push_dinner_hour, push_dinner_minute,
                   push_lunch_last_date, push_dinner_last_date, push_friday_last_date
            from user_profiles
            where push_enabled = true
            """
        )
    return [dict(r) for r in rows]


async def last_meal_at(user_id: int):
    """Retorna timestamptz da última refeição logada (ou None)."""
    async with pool().acquire() as conn:
        return await conn.fetchval(
            "select max(eaten_at) from meals where user_id=$1", user_id
        )


async def mark_push_sent(user_id: int, kind: str, local_date) -> None:
    """kind: 'lunch' | 'dinner' | 'friday'"""
    col = {"lunch": "push_lunch_last_date",
           "dinner": "push_dinner_last_date",
           "friday": "push_friday_last_date"}[kind]
    async with pool().acquire() as conn:
        await conn.execute(
            f"update user_profiles set {col}=$1 where user_id=$2", local_date, user_id,
        )


async def insert_exercise(user_id: int, activity: str, kcal_burned: int,
                          duration_min: int | None = None,
                          distance_km: float | None = None,
                          avg_hr: int | None = None,
                          done_at=None,
                          source: str = "agent",
                          photo_file_id: str | None = None,
                          notes: str | None = None) -> int:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into exercises (user_id, activity, kcal_burned, duration_min,
                                    distance_km, avg_hr, done_at, source,
                                    photo_file_id, notes)
            values ($1,$2,$3,$4,$5,$6,coalesce($7, now()),$8,$9,$10)
            returning id
            """,
            user_id, activity, kcal_burned, duration_min, distance_km, avg_hr,
            done_at, source, photo_file_id, notes,
        )
    return row["id"]


async def _user_tz(user_id: int) -> str:
    """Retorna tz IANA do user (default America/Sao_Paulo)."""
    async with pool().acquire() as conn:
        tz = await conn.fetchval(
            "select coalesce(timezone, 'America/Sao_Paulo') from user_profiles where user_id=$1",
            user_id,
        )
    return tz or "America/Sao_Paulo"


def _day_start_utc(tz_name: str) -> datetime:
    """Início do dia local do user, retornado como datetime UTC."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


async def list_exercises_today(user_id: int) -> list[dict]:
    tz = await _user_tz(user_id)
    start = _day_start_utc(tz)
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            select id, activity, kcal_burned, duration_min, done_at
            from exercises where user_id=$1 and done_at >= $2
            order by done_at
            """,
            user_id, start,
        )
    return [dict(r) for r in rows]


async def sum_today_meals(user_id: int) -> dict:
    tz = await _user_tz(user_id)
    start = _day_start_utc(tz)
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            select coalesce(sum(kcal), 0) as kcal,
                   coalesce(sum(protein_g), 0) as protein_g,
                   coalesce(sum(carbs_g), 0) as carbs_g,
                   coalesce(sum(fat_g), 0) as fat_g
            from meals where user_id=$1 and eaten_at >= $2
            """,
            user_id, start,
        )
    return {k: float(v) for k, v in dict(row).items()}


async def sum_today_exercises(user_id: int) -> int:
    tz = await _user_tz(user_id)
    start = _day_start_utc(tz)
    async with pool().acquire() as conn:
        v = await conn.fetchval(
            "select coalesce(sum(kcal_burned), 0) from exercises where user_id=$1 and done_at >= $2",
            user_id, start,
        )
    return int(v or 0)


async def period_summary(user_id: int, days: int) -> dict:
    """Resumo dia-a-dia dos últimos N dias (no tz local do user)."""
    from zoneinfo import ZoneInfo
    tz_name = await _user_tz(user_id)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Sao_Paulo")
        tz_name = "America/Sao_Paulo"

    today_local = datetime.now(tz).date()
    start_local_date = today_local - timedelta(days=days - 1)
    start_local_dt = datetime.combine(start_local_date,
                                       datetime.min.time(), tzinfo=tz)
    start_utc = start_local_dt.astimezone(timezone.utc)

    async with pool().acquire() as conn:
        meal_rows = await conn.fetch(
            f"""
            select date(eaten_at at time zone '{tz_name}') as d,
                   sum(kcal) as kcal,
                   sum(protein_g) as protein_g
            from meals where user_id=$1 and eaten_at >= $2
            group by 1 order by 1
            """,
            user_id, start_utc,
        )
        ex_rows = await conn.fetch(
            f"""
            select date(done_at at time zone '{tz_name}') as d,
                   sum(kcal_burned) as kcal
            from exercises where user_id=$1 and done_at >= $2
            group by 1 order by 1
            """,
            user_id, start_utc,
        )

    by_day_intake = {r["d"]: float(r["kcal"] or 0) for r in meal_rows}
    by_day_protein = {r["d"]: float(r["protein_g"] or 0) for r in meal_rows}
    by_day_burned = {r["d"]: int(r["kcal"] or 0) for r in ex_rows}

    per_day = []
    for offset in range(days - 1, -1, -1):
        d = today_local - timedelta(days=offset)
        intake = by_day_intake.get(d, 0.0)
        burned = by_day_burned.get(d, 0)
        per_day.append({
            "date": d.isoformat(),
            "intake_kcal": round(intake, 0),
            "intake_protein_g": round(by_day_protein.get(d, 0.0), 0),
            "burned_kcal": burned,
            "net_kcal": round(intake - burned, 0),
        })

    totals = {
        "intake_kcal": round(sum(d["intake_kcal"] for d in per_day), 0),
        "burned_kcal": sum(d["burned_kcal"] for d in per_day),
        "net_kcal": round(sum(d["net_kcal"] for d in per_day), 0),
    }
    n = max(1, len(per_day))
    averages = {
        "intake_per_day": round(totals["intake_kcal"] / n, 0),
        "burned_per_day": round(totals["burned_kcal"] / n, 0),
        "net_per_day": round(totals["net_kcal"] / n, 0),
    }
    return {"per_day": per_day, "totals": totals, "averages": averages}


async def pop_pending_chart(conv_id: int) -> bytes | None:
    """Lê e remove o gráfico pendente do scratchpad (set por generate_report_chart)."""
    import base64
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "select scratchpad from conversations where id=$1", conv_id
        )
        if not row:
            return None
        sp = row["scratchpad"] if isinstance(row["scratchpad"], dict) else json.loads(row["scratchpad"] or "{}")
        b64 = sp.get("_pending_chart_b64")
        if not b64:
            return None
        await conn.execute(
            "update conversations set scratchpad = scratchpad - '_pending_chart_b64' where id=$1",
            conv_id,
        )
    return base64.b64decode(b64)


async def close_active_conversation(user_id: int) -> None:
    """Marca conversa ativa do usuário como closed. Próxima msg cria nova."""
    async with pool().acquire() as conn:
        await conn.execute(
            "update conversations set state='closed' where user_id=$1 and state='active'",
            user_id,
        )
