"""Chamadas ao OpenRouter: visão (identificação prato + cardápio) e rerank."""
import base64
import json
import os
from typing import Any

import httpx

from agent.prompts import (
    VISION_SYSTEM_PROMPT, MENU_PARSE_SYSTEM_PROMPT, RERANK_SYSTEM_PROMPT
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


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


def _image_message(prompt: str, image_bytes: bytes, mime_type: str) -> list[dict]:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


async def identify_items(image_bytes: bytes, mime_type: str, model: str) -> tuple[dict, dict]:
    """Foto de prato → items + porção. Não calcula macros."""
    messages = [{"role": "system", "content": VISION_SYSTEM_PROMPT}] + _image_message(
        "Identifique os alimentos.", image_bytes, mime_type
    )
    raw = await _post(model, messages)
    content = raw["choices"][0]["message"]["content"]
    analysis = json.loads(content)
    for item in analysis.get("items", []):
        if item.get("portion_g") is not None:
            try:
                item["portion_g"] = float(item["portion_g"])
            except (TypeError, ValueError):
                item["portion_g"] = None
        item.setdefault("medida_caseira", None)
        item.setdefault("is_processed", False)
        item.setdefault("cooking_method", "other")
        if item.get("confidence") not in ("low", "med", "high"):
            item["confidence"] = "low"
    return analysis, raw


async def parse_menu_image(image_bytes: bytes, mime_type: str, model: str) -> dict:
    """Foto de cardápio → lista de pratos."""
    messages = [{"role": "system", "content": MENU_PARSE_SYSTEM_PROMPT}] + _image_message(
        "Extraia os itens deste cardápio.", image_bytes, mime_type
    )
    raw = await _post(model, messages)
    content = raw["choices"][0]["message"]["content"]
    return json.loads(content)


async def rerank(name: str, candidates: list[dict], model: str) -> int | None:
    """Desempate entre top-N do trigram."""
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
