"""Testes do router NLU. Mocka image_classifier — não faz chamada de rede.

Rodar:
    python -m pytest tests/test_router.py -v
ou:
    python tests/test_router.py
"""
import asyncio
import os
import sys
from unittest.mock import patch, AsyncMock

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import router


# ============================================================
# Helpers
# ============================================================
async def _classify_with_image(text, image_kind, image_conf=0.9, history=None, pending_kind=None):
    """Roda router.classify com image_classifier mockado."""
    with patch("agent.image_classifier.classify",
                new=AsyncMock(return_value=(image_kind, image_conf))):
        return await router.classify(
            text=text,
            photo_bytes=b"fake_image_bytes" if image_kind else None,
            history=history or [],
            vision_model="test-model",
            pending_kind=pending_kind,
        )


async def _classify_text_only(text, history=None, pending_kind=None):
    """Roda router.classify só com texto, sem foto."""
    return await router.classify(
        text=text, photo_bytes=None, history=history or [],
        vision_model="test-model",
        pending_kind=pending_kind,
    )


# ============================================================
# Bug A: foto cardápio + caption descritivo
# ============================================================
async def test_bug_a_menu_with_descriptive_caption():
    """User mandou foto de cardápio com 'Posso comer dois?...' →
    NÃO deve chamar estimate_meal_from_photo nem log_meal."""
    decision = await _classify_with_image(
        text="Posso comer dois? Com pão árabe, todos os ingredientes e tahine",
        image_kind="menu", image_conf=0.9,
    )
    assert decision.intent == "choose_from_menu", \
        f"esperado choose_from_menu, veio {decision.intent}"
    assert decision.allowed_tools is not None, "deveria restringir tools"
    assert "estimate_meal_from_photo" not in decision.allowed_tools, \
        "NÃO deveria deixar estimar foto como refeição"
    assert "log_meal" not in decision.allowed_tools, \
        "NÃO deveria deixar logar refeição"
    assert "parse_menu" in decision.allowed_tools, \
        "deveria deixar parsear o cardápio"
    print("[OK] bug_a_menu_with_descriptive_caption")


# ============================================================
# Bug B: foto cardápio + "Escolha por mim"
# ============================================================
async def test_bug_b_choose_for_me():
    """User mandou cardápio + 'Escolha por mim' → NÃO deve logar a feijoada ilustrativa."""
    decision = await _classify_with_image(
        text="Escolha por mim",
        image_kind="menu", image_conf=0.95,
    )
    assert decision.intent == "choose_from_menu"
    assert decision.allowed_tools is not None
    assert "log_meal" not in decision.allowed_tools, "NUNCA logar nesse intent"
    assert "estimate_meal_from_photo" not in decision.allowed_tools
    print("[OK] bug_b_choose_for_me")


# ============================================================
# Regressões — fluxos normais devem continuar funcionando
# ============================================================
async def test_plate_no_caption_logs_meal():
    """Foto de prato sem caption → log_meal_now."""
    decision = await _classify_with_image(
        text="", image_kind="plate", image_conf=0.9,
    )
    assert decision.intent == "log_meal_now"
    assert decision.allowed_tools is not None
    assert "estimate_meal_from_photo" in decision.allowed_tools
    assert "log_meal" in decision.allowed_tools
    print("[OK] plate_no_caption_logs_meal")


async def test_scale_photo_routes_to_weight():
    """Foto de balança → log_weight."""
    decision = await _classify_with_image(
        text="", image_kind="scale", image_conf=0.95,
    )
    assert decision.intent == "log_weight"
    assert "parse_scale_photo" in decision.allowed_tools
    assert "log_weight" in decision.allowed_tools
    print("[OK] scale_photo_routes_to_weight")


async def test_watch_photo_routes_to_exercise():
    """Foto de relógio → log_exercise."""
    decision = await _classify_with_image(
        text="", image_kind="watch", image_conf=0.92,
    )
    assert decision.intent == "log_exercise"
    assert "parse_watch_photo" in decision.allowed_tools
    print("[OK] watch_photo_routes_to_exercise")


async def test_weight_number_only_text():
    """Texto '98.5' → log_weight."""
    decision = await _classify_text_only(text="98.5")
    assert decision.intent == "log_weight"
    assert "log_weight" in decision.allowed_tools
    print("[OK] weight_number_only_text")


async def test_weight_with_kg_unit():
    """Texto '101,8 kg' → log_weight (vírgula decimal)."""
    decision = await _classify_text_only(text="101,8 kg")
    assert decision.intent == "log_weight"
    print("[OK] weight_with_kg_unit")


async def test_status_query():
    """'como tá meu dia?' → query_status."""
    decision = await _classify_text_only(text="como tá meu dia?")
    assert decision.intent == "query_status"
    assert "get_calorie_balance" in decision.allowed_tools
    print("[OK] status_query")


async def test_free_chat_falls_back():
    """Sem sinal forte → free_chat → libera tudo (allowed=None)."""
    decision = await _classify_text_only(text="oi tudo bem")
    assert decision.intent == "free_chat"
    assert decision.allowed_tools is None, "free_chat libera todas as tools"
    print("[OK] free_chat_falls_back")


async def test_ambiguous_plate_with_choose_question():
    """Foto de prato + 'posso comer?' → ambíguo → cai pro fallback (libera todas)."""
    decision = await _classify_with_image(
        text="posso comer?", image_kind="plate", image_conf=0.85,
    )
    # Combinação ambígua: imagem=plate (log_meal) mas texto=choose (menu)
    # Esperado: fallback seguro
    assert decision.confidence < 0.7
    assert decision.allowed_tools is None
    print("[OK] ambiguous_plate_with_choose_question")


async def test_image_classifier_low_confidence_falls_back():
    """Classifier retornou other com conf baixa → fallback open-schema."""
    decision = await _classify_with_image(
        text="", image_kind="other", image_conf=0.4,
    )
    assert decision.confidence < 0.7
    assert decision.allowed_tools is None
    print("[OK] image_classifier_low_confidence_falls_back")


async def test_onboarding_intent():
    """'quero definir minha meta' → onboarding."""
    decision = await _classify_text_only(text="quero definir minha meta")
    assert decision.intent == "onboarding"
    assert "set_profile" in decision.allowed_tools
    print("[OK] onboarding_intent")


# ============================================================
# Pending proposal (Sprint 1+2)
# ============================================================
async def test_sim_with_pending_meal_routes_to_confirm():
    """User responde 'Sim' quando há proposta de refeição pendente →
    intent confirm_pending, tools restritas a confirm_proposal/cancel_proposal."""
    d = await _classify_text_only("Sim", pending_kind="meal")
    assert d.intent == "confirm_pending", f"esperado confirm_pending, veio {d.intent}"
    assert d.allowed_tools is not None
    assert "confirm_proposal" in d.allowed_tools
    assert "log_meal" not in d.allowed_tools, \
        "log_meal NÃO deve estar no menu — força confirm_proposal"


async def test_nao_with_pending_meal_routes_to_cancel():
    d = await _classify_text_only("não", pending_kind="meal")
    assert d.intent == "cancel_pending", f"esperado cancel_pending, veio {d.intent}"
    assert "cancel_proposal" in (d.allowed_tools or set())


async def test_sim_without_pending_is_free_chat():
    """Sem pending, 'Sim' isolado não vira confirm_pending — fica ambíguo (free_chat)."""
    d = await _classify_text_only("Sim", pending_kind=None)
    assert d.intent != "confirm_pending", "não pode ativar confirm_pending sem pending"


async def test_loga_with_pending_routes_to_confirm():
    """Variações de 'sim': 'loga', 'ok', 'pode'."""
    for word in ["loga", "ok", "pode", "confirma", "vai", "salva"]:
        d = await _classify_text_only(word, pending_kind="meal")
        assert d.intent == "confirm_pending", \
            f"'{word}' com pending deve ser confirm_pending, veio {d.intent}"


async def test_long_response_with_pending_does_not_confirm():
    """User responde longo com pending pendente → NÃO confirma automaticamente."""
    d = await _classify_text_only(
        "Sim, mas adiciona uma maçã também",
        pending_kind="meal",
    )
    assert d.intent != "confirm_pending", \
        "frase longa após 'sim' não pode disparar confirm automático"


# ============================================================
# Runner
# ============================================================
ALL_TESTS = [
    test_bug_a_menu_with_descriptive_caption,
    test_bug_b_choose_for_me,
    test_plate_no_caption_logs_meal,
    test_scale_photo_routes_to_weight,
    test_watch_photo_routes_to_exercise,
    test_weight_number_only_text,
    test_weight_with_kg_unit,
    test_status_query,
    test_free_chat_falls_back,
    test_ambiguous_plate_with_choose_question,
    test_image_classifier_low_confidence_falls_back,
    test_onboarding_intent,
    test_sim_with_pending_meal_routes_to_confirm,
    test_nao_with_pending_meal_routes_to_cancel,
    test_sim_without_pending_is_free_chat,
    test_loga_with_pending_routes_to_confirm,
    test_long_response_with_pending_does_not_confirm,
]


async def _run_all():
    failures = []
    for t in ALL_TESTS:
        try:
            await t()
        except AssertionError as e:
            failures.append((t.__name__, str(e)))
            print(f"[FAIL] {t.__name__}: {e}")
        except Exception as e:
            failures.append((t.__name__, f"erro inesperado: {e!r}"))
            print(f"[ERR ] {t.__name__}: {e!r}")
    print()
    print("=" * 60)
    print(f"{len(ALL_TESTS) - len(failures)}/{len(ALL_TESTS)} passaram")
    if failures:
        print(f"FALHAS: {len(failures)}")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_run_all())
