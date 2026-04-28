"""Testes do cache TTL de 30s em classify_and_respond.

Não chamamos Gemini de verdade — só checamos a infraestrutura
de cache (helpers `_cache_get`/`_cache_set`).
"""

import time

import pytest

from src import intent as intent_module


@pytest.fixture(autouse=True)
def clear_cache():
    intent_module._CLASSIFY_CACHE.clear()
    yield
    intent_module._CLASSIFY_CACHE.clear()


def test_cache_get_returns_none_when_missing():
    assert intent_module._cache_get("foo") is None


def test_cache_set_then_get_returns_clone():
    intent_module._cache_set("hi", {"intent": "chat", "reply": "oi", "extracted_facts": []})
    out = intent_module._cache_get("hi")
    assert out is not None
    assert out["intent"] == "chat"
    assert out["reply"] == "oi"


def test_cache_strips_aux_dispatch_fields():
    intent_module._cache_set(
        "k",
        {
            "intent": "create_reminder",
            "reply": "ok",
            "extracted_facts": [],
            "_reminder_id": 99,  # auxiliar — NÃO deve voltar do cache
        },
    )
    out = intent_module._cache_get("k")
    assert "_reminder_id" not in out


def test_cache_returns_independent_copy():
    """Mutar o resultado do cache não polui a entrada armazenada."""
    intent_module._cache_set("k", {"intent": "chat", "reply": "x", "extracted_facts": []})
    first = intent_module._cache_get("k")
    first["extracted_facts"].append({"category": "fact", "content": "polui"})
    second = intent_module._cache_get("k")
    assert second["extracted_facts"] == []


def test_cache_expires_after_ttl():
    original_ttl = intent_module._CLASSIFY_CACHE_TTL_S
    intent_module._CLASSIFY_CACHE_TTL_S = 0.01
    try:
        intent_module._cache_set("k", {"intent": "chat", "extracted_facts": []})
        time.sleep(0.05)
        assert intent_module._cache_get("k") is None
    finally:
        intent_module._CLASSIFY_CACHE_TTL_S = original_ttl


def test_cache_evicts_oldest_when_full():
    original_max = intent_module._CLASSIFY_CACHE_MAX_ENTRIES
    intent_module._CLASSIFY_CACHE_MAX_ENTRIES = 3
    try:
        intent_module._cache_set("a", {"intent": "chat", "extracted_facts": []})
        intent_module._cache_set("b", {"intent": "chat", "extracted_facts": []})
        intent_module._cache_set("c", {"intent": "chat", "extracted_facts": []})
        intent_module._cache_set("d", {"intent": "chat", "extracted_facts": []})
        # 'a' deve ter sido descartada (mais antiga)
        assert intent_module._cache_get("a") is None
        assert intent_module._cache_get("d") is not None
    finally:
        intent_module._CLASSIFY_CACHE_MAX_ENTRIES = original_max
