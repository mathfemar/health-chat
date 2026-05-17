"""Chamadas ao OpenRouter: visão (identificação) e rerank (matching)."""
import base64
import json
import os
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VISION_SYSTEM_PROMPT = """Você identifica alimentos em fotos de refeições para uma base nutricional brasileira (TACO).

Sua tarefa: listar cada alimento visível, sua porção estimada em GRAMAS, e o estado/cocção (cru, cozido, grelhado, frito, assado).

REGRAS:
1. DECOMPONHA pratos compostos em ingredientes principais.
   Ex: "lasanha" → massa cozida, molho de tomate, carne moída, queijo muçarela.
   Ex: "sushi" → arroz, peixe (especifique tipo se possível), nori.
2. Use nomes SIMPLES e BRASILEIROS (frango, não "chicken"; feijão preto, não "black beans").
3. Especifique a cocção SEMPRE que possível: "arroz cozido", "frango grelhado", "batata frita".
4. Porção em gramas, estimada com referências visuais (talher, prato ~26cm, mão).
5. Se for um alimento processado/restaurante que não tem decomposição clara (ex: sorvete industrial, refrigerante), marque is_processed=true e forneça macros estimados.

Responda APENAS JSON válido, sem markdown, neste schema:
{
  "items": [
    {
      "name": "string em pt-BR, ex: 'arroz cozido'",
      "portion_g": number,
      "cooking_method": "raw" | "cooked" | "grilled" | "fried" | "roasted" | "other",
      "confidence": "low" | "med" | "high",
      "is_processed": boolean,
      "estimated_kcal_per_100g": number | null,
      "estimated_protein_per_100g": number | null,
      "estimated_carbs_per_100g": number | null,
      "estimated_fat_per_100g": number | null
    }
  ],
  "notes": "string opcional curta"
}

Os campos estimated_* só são preenchidos se is_processed=true (fallback quando a base não vai ter).
Se NÃO for comida, retorne items: []."""


RERANK_SYSTEM_PROMPT = """Você é um nutricionista. Dado o nome de um alimento e uma lista de candidatos de uma base, escolha qual candidato é o melhor match. Considere cocção (cru/cozido/grelhado/frito), parte do animal (peito/coxa), e tipo (integral/refinado).

Responda APENAS JSON:
{"match_id": <id do candidato> | null, "reason": "string curta"}

Retorne null se NENHUM candidato representa razoavelmente o alimento (ex: alimento é "iogurte grego" e candidatos só têm "leite", "queijo")."""


async def _post(model: str, messages: list[dict], json_mode: bool = True) -> dict:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/local/health-chat",
        "X-Title": "health-chat",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenRouter {r.status_code}: {r.text}")
        return r.json()


async def identify_items(image_bytes: bytes, mime_type: str, model: str) -> tuple[dict, dict]:
    """Retorna (analysis, raw). analysis = {items: [...], notes: str}."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    messages = [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Identifique os alimentos."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    raw = await _post(model, messages)
    content = raw["choices"][0]["message"]["content"]
    analysis = json.loads(content)
    # Normalização defensiva
    for item in analysis.get("items", []):
        item["portion_g"] = float(item.get("portion_g") or 0)
        item.setdefault("is_processed", False)
        item.setdefault("cooking_method", "other")
        if item["confidence"] not in ("low", "med", "high"):
            item["confidence"] = "low"
    return analysis, raw


async def rerank(name: str, candidates: list[dict], model: str) -> int | None:
    """candidates: [{id, name}]. Retorna id escolhido ou None."""
    cand_list = "\n".join(f"  {c['id']}: {c['name']}" for c in candidates)
    messages = [
        {"role": "system", "content": RERANK_SYSTEM_PROMPT},
        {"role": "user", "content": f"Alimento: '{name}'\n\nCandidatos:\n{cand_list}"},
    ]
    raw = await _post(model, messages)
    try:
        out = json.loads(raw["choices"][0]["message"]["content"])
        return out.get("match_id")
    except (json.JSONDecodeError, KeyError):
        return None
