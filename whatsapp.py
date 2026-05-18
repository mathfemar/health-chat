"""Adapter Twilio/WhatsApp.

Webhook FastAPI que recebe mensagens do Twilio, roda o agente e responde via REST API.
Compartilha estado com o bot do Telegram via DB (mesmo user_id quando o número
configurado em WHATSAPP_LINK_PHONE bate).

Endpoints:
  POST /twilio/webhook  → recebe mensagem do Twilio
  GET  /media/<token>   → serve imagens (gráficos) pra Twilio buscar
  GET  /healthz         → health check

Variáveis de ambiente:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_WHATSAPP_FROM        ex: "whatsapp:+14155238886" (sandbox) ou seu número aprovado
  WHATSAPP_LINK_PHONE         ex: "+5511987654321" — telefone do ALLOWED_USER_ID
  PUBLIC_BASE_URL             URL pública do servidor (Cloudflare Tunnel/ngrok)
  TWILIO_VALIDATE             "1" pra validar assinatura X-Twilio-Signature (default 1)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from html import unescape
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator

load_dotenv()

# Configura logging do app (uvicorn standalone não chama basicConfig do nosso código).
# Se bot.py já configurou (modo integrado), basicConfig é no-op.
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

import db
from agent import runtime as agent

log = logging.getLogger("health-chat.whatsapp")

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# Cache em memória de PNGs pra Twilio buscar via GET /media/<token>
# Twilio só precisa buscar 1x, então expiramos rápido.
_MEDIA_CACHE: dict[str, tuple[bytes, str, float]] = {}  # token -> (bytes, mime, expires_at)
_MEDIA_TTL_SECONDS = 600  # 10 min de sobra pra Twilio bater


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise RuntimeError(f"env var {name} obrigatória")
    return val


def _twilio_auth() -> tuple[str, str]:
    return _env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN")


def _phone_normalize(p: str) -> str:
    """Tira 'whatsapp:' prefix e normaliza pra '+E.164'."""
    p = (p or "").strip()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:") :]
    return p


def _allowed_phones() -> set[str]:
    """Lista normalizada de telefones autorizados (WHATSAPP_LINK_PHONE,
    aceita lista separada por vírgula pra suportar múltiplos números de teste)."""
    raw = os.environ.get("WHATSAPP_LINK_PHONE", "")
    return {_phone_normalize(p) for p in raw.split(",") if p.strip()}


def _resolve_user_id(from_phone: str) -> int | None:
    """Mapeia phone → user_id. Single-user: qualquer um dos telefones em
    WHATSAPP_LINK_PHONE (comma-sep) mapeia pro ALLOWED_USER_ID."""
    allowed = os.environ.get("ALLOWED_USER_ID")
    if not allowed:
        return None
    if _phone_normalize(from_phone) in _allowed_phones():
        try:
            return int(allowed)
        except ValueError:
            return None
    return None


# ============================================================
# Download/upload de mídia
# ============================================================

async def download_twilio_media(media_url: str) -> bytes | None:
    """Baixa MediaUrlN do Twilio (precisa basic auth)."""
    sid, tok = _twilio_auth()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(media_url, auth=(sid, tok))
        if r.status_code >= 400:
            log.warning("download twilio media falhou %s: %s", r.status_code, r.text[:200])
            return None
        return r.content


def stash_media(data: bytes, mime: str = "image/png") -> str:
    """Guarda bytes em cache e retorna URL pública que Twilio vai buscar."""
    token = secrets.token_urlsafe(16)
    _MEDIA_CACHE[token] = (data, mime, time.time() + _MEDIA_TTL_SECONDS)
    # purge expirados
    now = time.time()
    for k in list(_MEDIA_CACHE):
        if _MEDIA_CACHE[k][2] < now:
            _MEDIA_CACHE.pop(k, None)
    base = _env("PUBLIC_BASE_URL").rstrip("/")
    return f"{base}/media/{token}"


# ============================================================
# Envio via REST API
# ============================================================

async def send_whatsapp_message(
    to_phone: str, body: str | None = None, media_url: str | None = None
) -> dict:
    """Envia mensagem WhatsApp via Twilio REST API.
    to_phone aceita formato '+5511...' ou 'whatsapp:+5511...'.
    """
    sid, tok = _twilio_auth()
    from_ = _env("TWILIO_WHATSAPP_FROM")
    if not from_.startswith("whatsapp:"):
        from_ = f"whatsapp:{from_}"
    to_ = to_phone if to_phone.startswith("whatsapp:") else f"whatsapp:{_phone_normalize(to_phone)}"

    payload: dict[str, str] = {"From": from_, "To": to_}
    if body:
        payload["Body"] = body
    if media_url:
        payload["MediaUrl"] = media_url

    url = f"{TWILIO_API_BASE}/Accounts/{sid}/Messages.json"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            url,
            data=urlencode(payload),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(sid, tok),
        )
        if r.status_code >= 400:
            log.error("twilio send falhou %s: %s", r.status_code, r.text[:300])
        else:
            log.info("twilio send ok sid=%s", r.json().get("sid"))
        return {"status": r.status_code, "body": r.text}


# ============================================================
# Sanitização: HTML do Telegram → texto plano pro WhatsApp
# ============================================================

_TG_TO_WA = [
    (re.compile(r"</?(?:b|strong)>", re.I), "*"),
    (re.compile(r"</?(?:i|em)>", re.I), "_"),
    (re.compile(r"</?u>", re.I), ""),
    (re.compile(r"</?s>", re.I), "~"),
    (re.compile(r"</?code>", re.I), "`"),
    (re.compile(r"</?pre>", re.I), "```"),
]


def tg_html_to_whatsapp(text: str) -> str:
    """Converte HTML do Telegram pra formatação WhatsApp (*bold*, _italic_, `code`)."""
    if not text:
        return ""
    for rx, sub in _TG_TO_WA:
        text = rx.sub(sub, text)
    # Tira tags desconhecidas
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


# ============================================================
# FastAPI app
# ============================================================

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Init DB se ainda não foi inicializado (caso rode standalone, sem bot.py)
    try:
        n = await db.count_foods()
        log.info("DB já inicializado (%s alimentos).", n)
    except Exception:
        log.info("Inicializando DB...")
        await db.init()
        n = await db.count_foods()
        if n == 0:
            log.warning("Banco vazio! Rode: python import_taco.py")
        else:
            log.info("DB pronto. %s alimentos carregados.", n)

    if not all(
        os.environ.get(k)
        for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                  "TWILIO_WHATSAPP_FROM", "WHATSAPP_LINK_PHONE", "PUBLIC_BASE_URL")
    ):
        log.warning(
            "Faltam envs Twilio. O servidor sobe mas mensagens serão rejeitadas. "
            "Configure: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, "
            "WHATSAPP_LINK_PHONE, PUBLIC_BASE_URL."
        )
    yield
    try:
        await db.close()
    except Exception:
        pass


app = FastAPI(title="health-chat WhatsApp adapter", lifespan=_lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "whatsapp"}


@app.get("/media/{token}")
async def serve_media(token: str) -> Response:
    entry = _MEDIA_CACHE.get(token)
    if not entry:
        raise HTTPException(404, "media not found or expired")
    data, mime, expires = entry
    if expires < time.time():
        _MEDIA_CACHE.pop(token, None)
        raise HTTPException(410, "expired")
    return Response(content=data, media_type=mime)


def _validate_signature(request: Request, form: dict) -> bool:
    """Valida X-Twilio-Signature. Skipa se TWILIO_VALIDATE=0."""
    if os.environ.get("TWILIO_VALIDATE", "1") == "0":
        return True
    sig = request.headers.get("X-Twilio-Signature", "")
    if not sig:
        return False
    _, tok = _twilio_auth()
    validator = RequestValidator(tok)
    # URL pública (Twilio assina com a URL que ELE chamou, então usa PUBLIC_BASE_URL)
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    url = f"{base}{request.url.path}"
    return validator.validate(url, form, sig)


@app.post("/twilio/webhook")
async def twilio_webhook(request: Request) -> PlainTextResponse:
    """Webhook de mensagem recebida.

    Retorna TwiML vazio imediatamente e processa em background pra não
    bater no timeout de 15s do Twilio.
    """
    form_data = await request.form()
    form = {k: v for k, v in form_data.items() if isinstance(v, str)}

    if not _validate_signature(request, form):
        log.warning("assinatura twilio inválida — rejeitando")
        raise HTTPException(403, "invalid signature")

    from_phone = form.get("From", "")
    body = (form.get("Body") or "").strip()
    num_media = int(form.get("NumMedia", "0") or "0")
    media_url = form.get("MediaUrl0") if num_media > 0 else None
    media_type = form.get("MediaContentType0") if num_media > 0 else None

    user_id = _resolve_user_id(from_phone)
    if user_id is None:
        if not os.environ.get("ALLOWED_USER_ID"):
            log.warning("ALLOWED_USER_ID vazio no .env — não consigo mapear nenhum telefone")
        elif _phone_normalize(from_phone) not in _allowed_phones():
            log.info("telefone %s não está em WHATSAPP_LINK_PHONE — ignorando", from_phone)
        else:
            log.warning("ALLOWED_USER_ID=%r não é int válido", os.environ.get("ALLOWED_USER_ID"))
        # responde TwiML vazio — silenciosamente ignora
        return PlainTextResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response/>',
            media_type="application/xml",
        )

    log.info(
        "twilio msg: from=%s user_id=%s body=%r media=%s",
        from_phone, user_id, body[:50], media_type,
    )

    # Processa em background — webhook retorna na hora
    asyncio.create_task(
        _process_message(user_id, from_phone, body, media_url, media_type)
    )

    # TwiML vazio = "ok, recebi"
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response/>',
        media_type="application/xml",
    )


async def _process_message(
    user_id: int,
    from_phone: str,
    body: str,
    media_url: str | None,
    media_type: str | None,
) -> None:
    """Executa o agente e responde via REST API."""
    try:
        # Photo handling: usamos o próprio URL como photo_ref. O agente passa
        # esse ref de volta pro download_photo callable, que baixa via basic auth.
        photo_ref: str | None = None
        if media_url and media_type and media_type.startswith("image/"):
            photo_ref = media_url

        async def _dl(pid: str) -> bytes | None:
            # pid pode ser o URL do Twilio, ou o LLM pode passar algum lixo —
            # se não for URL, ignora.
            if not (pid.startswith("http://") or pid.startswith("https://")):
                return None
            return await download_twilio_media(pid)

        reply = await agent.run_turn(
            user_id=user_id,
            user_text=body,
            photo_file_id=photo_ref,
            download_photo=_dl,
            vision_model=os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"),
        )

        # Converte HTML do Telegram pra formatação WhatsApp
        text_wa = tg_html_to_whatsapp(reply or "(sem resposta)")

        await send_whatsapp_message(from_phone, body=text_wa)

        # Se o agente gerou gráfico, manda como anexo
        try:
            conv = await agent.get_or_create_conversation(user_id)
            chart_bytes = await db.pop_pending_chart(conv["id"])
        except Exception:
            log.exception("falha buscando chart pendente")
            chart_bytes = None

        if chart_bytes:
            try:
                url = stash_media(chart_bytes, "image/png")
                await send_whatsapp_message(from_phone, media_url=url)
            except RuntimeError as e:
                # PUBLIC_BASE_URL faltando — manda só aviso
                log.warning("não consegui mandar gráfico: %s", e)
                await send_whatsapp_message(
                    from_phone,
                    body="(gráfico gerado, mas PUBLIC_BASE_URL não configurado pra entregar via WhatsApp)",
                )
    except Exception as e:
        log.exception("erro processando msg WhatsApp")
        try:
            await send_whatsapp_message(from_phone, body=f"Erro: {e}")
        except Exception:
            pass
