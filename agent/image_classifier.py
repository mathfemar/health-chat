"""Classifica tipo de imagem (plate/menu/scale/watch/other) via Vision LLM.

Dedicado e barato: prompt mínimo, JSON enum + confidence. Custo ~$0.0005-0.001
por chamada (Gemini Flash). Roda 1x por foto que chega no bot, antes do
agente decidir qual tool chamar.
"""
import base64
import json
import logging
import os

import httpx

log = logging.getLogger("agent.image_classifier")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

CLASSIFY_PROMPT = """Classifique esta imagem em UMA categoria:
- "plate": foto de prato/refeição pronta pra comer
- "menu": foto de cardápio de restaurante (lista de pratos com preços)
- "scale": foto de balança mostrando peso
- "watch": foto de relógio fitness (Apple Watch, Garmin, Strava, etc)
- "other": qualquer outra coisa

Responda APENAS JSON: {"kind": "plate|menu|scale|watch|other", "confidence": 0.0-1.0}"""


VALID_KINDS = {"plate", "menu", "scale", "watch", "other"}


async def classify(image_bytes: bytes, model: str) -> tuple[str, float]:
    """Classifica uma foto. Retorna (kind, confidence).
    Em qualquer erro, retorna ('other', 0.0) — sinal fraco, router cai pro fallback.
    """
    if not image_bytes:
        return "other", 0.0
    try:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": CLASSIFY_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/local/health-chat",
            "X-Title": "health-chat",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            kind = data.get("kind", "other")
            conf = float(data.get("confidence", 0.0))
            if kind not in VALID_KINDS:
                log.warning("kind inválido retornado: %r — tratando como 'other'", kind)
                kind = "other"
            log.info("image_classifier: kind=%s conf=%.2f", kind, conf)
            return kind, conf
    except Exception as e:
        log.exception("image_classifier falhou: %s", e)
        return "other", 0.0
