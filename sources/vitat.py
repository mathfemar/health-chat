"""Cliente Vitat (vitat.com.br) — busca on-demand de alimentos.

Vitat usa Next.js com endpoints /_next/data/<buildId>/...json. O buildId
rotaciona a cada deploy; extraímos da home e cacheamos por 1h.

Bug conhecido (lado deles): quando medida='gramas', valores vêm por 1g
apesar do campo 'quantidade: 100'. Aplicamos correção ×100.
"""
import re
import time
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

BASE = "https://vitat.com.br"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_build_id: str | None = None
_build_id_expires: float = 0.0
_BUILD_ID_TTL_S = 3600  # 1h


@dataclass
class VitatHit:
    id: int
    name: str
    default_measure: str | None = None


@dataclass
class VitatPortion:
    name: str            # "filé pequeno", "gramas"
    grams: float | None  # quantos g essa porção representa, se conhecido
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


@dataclass
class VitatFood:
    id: int
    name: str
    # macros POR 100g (já corrigido o bug ×100)
    kcal_100g: float
    protein_100g: float
    carbs_100g: float
    fat_100g: float
    fiber_100g: float
    portions: list[VitatPortion]


async def _get_build_id(client: httpx.AsyncClient) -> str:
    """Extrai buildId da home (cacheado por 1h)."""
    global _build_id, _build_id_expires
    now = time.time()
    if _build_id and now < _build_id_expires:
        return _build_id

    r = await client.get(f"{BASE}/", headers=HEADERS, follow_redirects=True)
    r.raise_for_status()
    # Procura {"buildId":"xxxx"} no HTML
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', r.text)
    if not m:
        raise RuntimeError("Não consegui extrair buildId do Vitat (formato mudou?)")
    _build_id = m.group(1)
    _build_id_expires = now + _BUILD_ID_TTL_S
    log.info("Vitat buildId: %s", _build_id)
    return _build_id


async def search(query: str, max_results: int = 8) -> list[VitatHit]:
    """Busca alimentos no Vitat. Retorna até max_results hits."""
    async with httpx.AsyncClient(timeout=20) as client:
        build = await _get_build_id(client)
        url = (
            f"{BASE}/_next/data/{build}/alimentacao/busca-de-alimentos/busca.json"
            f"?q={quote(query)}"
        )
        r = await client.get(url, headers=HEADERS)
        if r.status_code == 404:
            # buildId pode ter expirado, força refresh
            global _build_id_expires
            _build_id_expires = 0
            return await search(query, max_results)
        r.raise_for_status()
        data = r.json()

    # Schema: pageProps.alimentos = [{codigo, descricao, medida, ...}]
    hits = []
    for item in data.get("pageProps", {}).get("alimentos", []):
        if item.get("tipo") != "ali":  # ignora receitas, só alimentos
            continue
        hits.append(VitatHit(
            id=int(item["codigo"]),
            name=item["descricao"],
            default_measure=item.get("medida"),
        ))
        if len(hits) >= max_results:
            break
    return hits


async def fetch_food(food_id: int, slug_hint: str | None = None) -> VitatFood:
    """Busca detalhe nutricional + porções. slug_hint melhora o cache hit
    do Vitat mas não é obrigatório."""
    # O endpoint quer slug, mas o id no início basta na prática.
    # Se temos o slug do search, usamos; senão tentamos só com id.
    slug = slug_hint or f"{food_id}-alimento"
    async with httpx.AsyncClient(timeout=20) as client:
        build = await _get_build_id(client)
        url = (
            f"{BASE}/_next/data/{build}/alimentacao/busca-de-alimentos/alimentos/"
            f"{quote(slug)}.json?id={quote(slug)}"
        )
        r = await client.get(url, headers=HEADERS)
        if r.status_code == 404:
            global _build_id_expires
            _build_id_expires = 0
            # tenta uma vez de novo com build novo
            build = await _get_build_id(client)
            url = (
                f"{BASE}/_next/data/{build}/alimentacao/busca-de-alimentos/alimentos/"
                f"{quote(slug)}.json?id={quote(slug)}"
            )
            r = await client.get(url, headers=HEADERS)
        r.raise_for_status()
        data = r.json()

    foods = data.get("pageProps", {}).get("foods")
    if not foods:
        raise RuntimeError(f"Vitat retornou sem foods pra id={food_id}")

    # Macros por 100g vêm no campo top-level
    # BUG: a entrada 'gramas' no array macroNutrientes vem por 1g.
    # Mas os campos top-level (proteína, gordurasTotais, etc) JÁ são por 100g
    # — exceto 'calorias' (que vem 13 — kcal por 1g x 100? estranho). Usamos 'caloria' (singular)
    # que confere com per-100g (116 pra salmão cru).
    kcal_100g = float(foods.get("caloria", foods.get("calorias", 0) * 100))
    protein_100g = float(foods.get("proteína", foods.get("proteina", 0)))
    carbs_100g = float(foods.get("carboidratos", 0))
    fat_100g = float(foods.get("gordurasTotais", 0))
    fiber_100g = float(foods.get("fibra", 0))

    # Porções vêm no macroNutrientes
    portions: list[VitatPortion] = []
    medida_padrao = foods.get("medidaPadrao")  # gramatura da porção padrão
    for mn in foods.get("macroNutrientes", []) or []:
        medida = mn.get("medida", "")
        is_grams = medida.lower().strip() == "gramas"
        # Bug ×100 só na entrada de gramas
        factor = 100.0 if is_grams else 1.0
        portions.append(VitatPortion(
            name=medida,
            grams=float(medida_padrao) if (not is_grams and medida_padrao) else (
                float(mn.get("quantidade", 100)) if is_grams else None
            ),
            kcal=float(mn.get("calorias", 0)) * factor,
            protein_g=float(mn.get("proteinas", 0)) * factor,
            carbs_g=float(mn.get("carboidratos", 0)) * factor,
            fat_g=float(mn.get("GordurasTotais", 0)) * factor,
        ))

    return VitatFood(
        id=food_id,
        name=foods["nome"],
        kcal_100g=kcal_100g,
        protein_100g=protein_100g,
        carbs_100g=carbs_100g,
        fat_100g=fat_100g,
        fiber_100g=fiber_100g,
        portions=portions,
    )
