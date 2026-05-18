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


BUILD_ID_PROBE_PATHS = (
    "/alimentacao/busca-de-alimentos/",   # tem "buildId":"..." no __NEXT_DATA__
    "/alimentacao/",
    "/",
)


async def _get_build_id(client: httpx.AsyncClient, force: bool = False) -> str:
    """Extrai buildId. Tenta múltiplas páginas, com fallback para _next/static/<HASH>."""
    global _build_id, _build_id_expires
    now = time.time()
    if not force and _build_id and now < _build_id_expires:
        return _build_id

    last_err: Exception | None = None
    for path in BUILD_ID_PROBE_PATHS:
        try:
            r = await client.get(f"{BASE}{path}", headers=HEADERS,
                                 follow_redirects=True, timeout=15)
            r.raise_for_status()
            html = r.text
            # 1ª tentativa: padrão canônico do __NEXT_DATA__
            m = re.search(r'"buildId"\s*:\s*"([A-Za-z0-9_-]+)"', html)
            # 2ª tentativa: fallback via _next/static/<HASH>/...
            if not m:
                m = re.search(r'/_next/static/([A-Za-z0-9_-]{15,})/', html)
            if m:
                _build_id = m.group(1)
                _build_id_expires = now + _BUILD_ID_TTL_S
                log.info("Vitat buildId=%s (extraído de %s)", _build_id, path)
                return _build_id
        except Exception as e:
            last_err = e
            log.warning("falha buscando buildId em %s: %s", path, e)
            continue
    raise RuntimeError(f"Não consegui extrair buildId do Vitat (último erro: {last_err})")


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

    # Schema: pageProps.foods.results = [{codigo, descricao, medida, tipo, ...}]
    foods_obj = data.get("pageProps", {}).get("foods", {}) or {}
    results = foods_obj.get("results", []) if isinstance(foods_obj, dict) else []
    hits = []
    for item in results:
        if item.get("tipo") != "ali":   # 'ali' = alimento; 'rec' = receita (pula)
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

    def _n(v) -> float:
        """Vitat usa None/null/string em vários campos. Normaliza pra float."""
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    # Macros POR 100g.
    # BUG conhecido Vitat: campo 'calorias' (plural) vem POR 1g (ex: salmão=1.7).
    #   Para corrigir: × 100.
    # Campo 'caloria' (singular) vem POR 100g já (ex: salmão=116). Preferimos esse.
    # Outros macros top-level (proteína, gordurasTotais, carboidratos, fibra) vêm POR 100g.
    cal_singular = foods.get("caloria")
    cal_plural = foods.get("calorias")
    if cal_singular is not None:
        kcal_100g = _n(cal_singular)
    elif cal_plural is not None:
        kcal_100g = _n(cal_plural) * 100   # corrige o bug
    else:
        kcal_100g = 0.0

    # Acentos no JSON são literais Unicode, mas alguns alimentos têm 'proteina' sem acento. Tenta ambos.
    protein_100g = _n(foods.get("proteína", foods.get("proteina", 0)))
    carbs_100g = _n(foods.get("carboidratos", 0))
    fat_100g = _n(foods.get("gordurasTotais", 0))
    fiber_100g = _n(foods.get("fibra", 0))

    # Porções vêm no macroNutrientes
    portions: list[VitatPortion] = []
    medida_padrao = foods.get("medidaPadrao")  # gramatura da porção padrão
    for mn in foods.get("macroNutrientes", []) or []:
        medida = (mn.get("medida") or "").strip()
        if not medida:
            continue
        is_grams = medida.lower() == "gramas"
        # Bug ×100 só na entrada de gramas (valores vêm por 1g em vez de 100g)
        factor = 100.0 if is_grams else 1.0
        grams_for_portion = None
        if is_grams:
            grams_for_portion = _n(mn.get("quantidade", 100)) or 100.0
        elif medida_padrao:
            grams_for_portion = _n(medida_padrao)
        portions.append(VitatPortion(
            name=medida,
            grams=grams_for_portion,
            kcal=_n(mn.get("calorias", 0)) * factor,
            protein_g=_n(mn.get("proteinas", 0)) * factor,
            carbs_g=_n(mn.get("carboidratos", 0)) * factor,
            fat_g=_n(mn.get("GordurasTotais", 0)) * factor,
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
