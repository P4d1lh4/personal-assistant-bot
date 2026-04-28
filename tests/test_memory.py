from src.memory import add_memory, list_memories, search_memories


def test_add_memory_returns_id():
    mid = add_memory("fact", "trabalho na Seaway")
    assert mid > 0


def test_add_memory_dedup_returns_existing_id():
    mid1 = add_memory("fact", "trabalho na Seaway")
    mid2 = add_memory("fact", "trabalho na Seaway")
    assert mid1 == mid2  # mesmo conteúdo retorna id antigo


def test_add_memory_dedup_case_insensitive():
    mid1 = add_memory("fact", "Trabalho na Seaway")
    mid2 = add_memory("fact", "trabalho NA seaway")
    assert mid1 == mid2


def test_add_memory_different_categories_not_dedup():
    mid1 = add_memory("fact", "café preto")
    mid2 = add_memory("preference", "café preto")
    assert mid1 != mid2


def test_list_memories_filters_by_category():
    add_memory("fact", "fato 1")
    add_memory("preference", "pref 1")
    add_memory("fact", "fato 2")
    rows = list_memories(category="fact")
    assert len(rows) == 2
    rows = list_memories()  # tudo
    assert len(rows) == 3


def test_search_memories_finds_substring():
    add_memory("fact", "trabalho na Seaway desde 2020")
    add_memory("fact", "moro em São Paulo")
    rows = search_memories("seaway")  # case-insensitive
    assert len(rows) == 1
    assert "Seaway" in rows[0]["content"]


def test_search_memories_empty_query_returns_empty():
    add_memory("fact", "qualquer coisa")
    assert search_memories("") == []
    assert search_memories("   ") == []
