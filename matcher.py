"""Match nome de alimento (vindo da Vision LLM) → linha na tabela `foods`.

Cascata:
  1. food_aliases (exato após normalizar) — aprendido das correções do user
  2. pg_trgm + unaccent (similarity)
  3. LLM rerank (top-5 do trigram) se top1 < threshold ou empate
"""
import os
import re
import unicodedata
from dataclasses import dataclass

import db
import llm

TRGM_THRESHOLD = 0.35       # mínimo absoluto pra considerar candidato
HIGH_CONF_THRESHOLD = 0.55  # acima disso, aceita sem rerank
GAP_FOR_TIEBREAK = 0.08     # se top1 - top2 < isso, considera empate e rerank


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass
class Match:
    food_id: int | None
    food_name: str | None
    score: float
    method: str               # 'alias' | 'trgm' | 'rerank' | 'none'
    alternatives: list[dict]  # [{id, name, score}]


async def _alias_lookup(conn, alias_norm: str) -> int | None:
    return await conn.fetchval(
        "select food_id from food_aliases where alias = $1 limit 1", alias_norm
    )


async def _trgm_candidates(conn, name_norm: str, k: int = 5) -> list[dict]:
    rows = await conn.fetch(
        """
        select id, name,
               similarity(name_normalized, $1) as sim
        from foods
        where name_normalized % $1
        order by sim desc
        limit $2
        """,
        name_norm,
        k,
    )
    return [dict(r) for r in rows]


async def match_one(name: str) -> Match:
    pool = db.pool()
    name_norm = normalize(name)
    async with pool.acquire() as conn:
        # 1. alias
        food_id = await _alias_lookup(conn, name_norm)
        if food_id:
            row = await conn.fetchrow("select id, name from foods where id=$1", food_id)
            if row:
                return Match(row["id"], row["name"], 1.0, "alias", [])

        # 2. trgm
        cands = await _trgm_candidates(conn, name_norm)
        if not cands:
            return Match(None, None, 0.0, "none", [])

        alternatives = [{"id": c["id"], "name": c["name"], "score": float(c["sim"])} for c in cands]
        top = cands[0]
        top_score = float(top["sim"])

        # confiança alta: aceita direto
        if top_score >= HIGH_CONF_THRESHOLD:
            second_score = float(cands[1]["sim"]) if len(cands) > 1 else 0.0
            if top_score - second_score >= GAP_FOR_TIEBREAK:
                return Match(top["id"], top["name"], top_score, "trgm", alternatives)

    # 3. rerank (fora do conn pra não segurar conexão durante chamada HTTP)
    if top_score < TRGM_THRESHOLD:
        # mesmo o melhor é ruim demais — tenta rerank pra ver se modelo encontra algo
        # mas se nem trgm achou nada com 0.35, provavelmente é "no match"
        return Match(None, None, top_score, "none", alternatives)

    rerank_model = os.environ.get("OPENROUTER_RERANK_MODEL", "google/gemma-2-9b-it:free")
    try:
        chosen_id = await llm.rerank(name, [{"id": c["id"], "name": c["name"]} for c in cands], rerank_model)
    except Exception:
        chosen_id = top["id"]  # fallback: confia no trgm

    if chosen_id is None:
        return Match(None, None, top_score, "none", alternatives)

    chosen = next((c for c in cands if c["id"] == chosen_id), None)
    if not chosen:
        return Match(None, None, top_score, "none", alternatives)
    return Match(chosen["id"], chosen["name"], float(chosen["sim"]), "rerank", alternatives)


async def save_alias(name: str, food_id: int, user_id: int | None = None) -> None:
    name_norm = normalize(name)
    pool = db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into food_aliases (alias, food_id, user_id)
            values ($1, $2, $3)
            on conflict (alias) do update set food_id = excluded.food_id
            """,
            name_norm,
            food_id,
            user_id,
        )
