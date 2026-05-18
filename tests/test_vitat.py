"""Teste manual do cliente Vitat. Roda: python tests/test_vitat.py

Valida:
1. buildId é extraído
2. search() retorna resultados pra alimentos comuns E nicho
3. fetch_food() retorna macros corretos (com bug ×100 corrigido)
"""
import asyncio
import logging
import os
import sys

# Windows console em UTF-8 pra suportar emoji
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sources import vitat


# Casos: nome -> dicas de expectativa
CASES = [
    ("salmão", "espera achar 'salmão cru'"),
    ("arroz", "achar arroz japonês ou similar"),
    ("kani kama", "tipico de sushi, talvez Vitat tenha"),
    ("sunomono", "salada japonesa"),
    ("cream cheese", "industrial comum"),
    ("ervilha wasabi", "snack japonês"),
    ("shoyu", "molho japonês"),
    ("batata doce chips", "snack"),
    ("frango grelhado", "comum"),
    ("whey protein", "suplemento"),
]


async def run():
    print("=" * 70)
    print("TESTE 1: extração de buildId")
    print("=" * 70)
    import httpx
    async with httpx.AsyncClient() as c:
        try:
            bid = await vitat._get_build_id(c, force=True)
            print(f"✅ buildId: {bid}")
        except Exception as e:
            print(f"❌ FALHOU: {e}")
            return

    print()
    print("=" * 70)
    print("TESTE 2: search() pra cada termo")
    print("=" * 70)
    results = {}
    for query, hint in CASES:
        try:
            hits = await vitat.search(query, max_results=4)
            if hits:
                print(f"✅ '{query}' → {len(hits)} hits")
                for h in hits[:3]:
                    print(f"      [{h.id}] {h.name}  (medida: {h.default_measure})")
                results[query] = hits
            else:
                print(f"⚠️  '{query}' → 0 hits  ({hint})")
                results[query] = []
        except Exception as e:
            print(f"❌ '{query}' → ERRO: {e}")
            results[query] = None

    print()
    print("=" * 70)
    print("TESTE 3: fetch_food() em 3 hits (1 comum, 1 industrial, 1 raro)")
    print("=" * 70)
    to_fetch = []
    if results.get("salmão"):
        to_fetch.append(("salmão", results["salmão"][0]))
    if results.get("cream cheese"):
        to_fetch.append(("cream cheese", results["cream cheese"][0]))
    if results.get("kani kama"):
        to_fetch.append(("kani kama", results["kani kama"][0]))

    for query, hit in to_fetch:
        try:
            # passa slug_hint pro fetch usar nome correto
            slug_norm = hit.name.lower().replace(" ", "-").replace(",", "")
            slug = f"{hit.id}-{slug_norm}"
            food = await vitat.fetch_food(hit.id, slug_hint=slug)
            print(f"\n✅ '{query}' → ID {food.id}: {food.name}")
            print(f"    Por 100g: {food.kcal_100g:.1f} kcal | "
                  f"P {food.protein_100g:.1f}g | "
                  f"C {food.carbs_100g:.1f}g | "
                  f"G {food.fat_100g:.1f}g")
            print(f"    Porções: {len(food.portions)} mapeadas")
            for p in food.portions[:5]:
                print(f"      • {p.name}: {p.kcal:.0f} kcal "
                      f"(P {p.protein_g:.1f} | C {p.carbs_g:.1f} | G {p.fat_g:.1f})")
            # Sanity check: salmão cru deve ter ~116 kcal/100g (Tab.Philippi)
            if "salmão" in query.lower() and "cru" in food.name.lower():
                if 100 < food.kcal_100g < 180:
                    print(f"    ✅ kcal/100g realista pra salmão cru ({food.kcal_100g:.0f})")
                else:
                    print(f"    ⚠️  kcal/100g suspeito: {food.kcal_100g}")
        except Exception as e:
            print(f"❌ fetch_food({hit.id}, '{hit.name}') falhou: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 70)
    print("RESUMO")
    print("=" * 70)
    found = sum(1 for r in results.values() if r)
    print(f"Cobertura: {found}/{len(CASES)} termos retornaram hits")


if __name__ == "__main__":
    asyncio.run(run())
