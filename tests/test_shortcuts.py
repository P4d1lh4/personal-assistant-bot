from src import activities, medications
from src.shortcuts import _normalize, match


# ---------- normalização ----------

def test_normalize_lowercases_and_strips_accents():
    assert _normalize("Memórias") == "memorias"
    assert _normalize("  Como Tô?  ") == "como to"
    assert _normalize("Ações!") == "acoes"


def test_normalize_keeps_inner_spacing():
    assert _normalize("Meus  Treinos") == "meus  treinos"


# ---------- shortcuts estáticos ----------

def test_match_static_list_workouts():
    out = match("meus treinos")
    assert out is not None
    assert out["intent"] == "list_workouts"
    assert out["reply"] == ""
    assert out["extracted_facts"] == []


def test_match_status_aliases():
    for phrase in ["status", "como tô", "panorama", "resumo"]:
        out = match(phrase)
        assert out is not None
        assert out["intent"] == "show_status"


def test_match_show_workout_today():
    out = match("treino de hoje")
    assert out["intent"] == "show_workout_schedule"
    assert out["scope"] == "today"


def test_match_finish_workout_session():
    out = match("acabei")
    assert out["intent"] == "finish_workout_session"


def test_match_handles_punctuation():
    out = match("status?")
    assert out["intent"] == "show_status"


def test_no_match_returns_none():
    assert match("supino 60 por 10") is None
    assert match("frase qualquer aleatória") is None
    assert match("") is None


def test_match_lembretes_aliases():
    for phrase in ["agenda", "meus lembretes", "lembretes"]:
        out = match(phrase)
        assert out["intent"] == "list_reminders"


# ---------- shortcuts dinâmicos (solo) ----------

def test_match_solo_med_with_one_active():
    medications.create("creatina", "0 23 * * *")
    out = match("tomei")
    assert out is not None
    assert out["intent"] == "track_medication"
    assert out["medication_name"] == "creatina"


def test_match_solo_med_with_multiple_falls_through():
    medications.create("creatina", "0 23 * * *")
    medications.create("whey", "0 8 * * *")
    out = match("tomei")
    # Múltiplos ativos → não infere → cai pra Gemini
    assert out is None


def test_match_solo_med_with_zero_active_falls_through():
    out = match("tomei")
    assert out is None


def test_match_solo_activity_with_one_active():
    activities.create("correr", "física")
    out = match("fiz")
    assert out is not None
    assert out["intent"] == "track_activity"
    assert out["activity_name"] == "correr"
    assert out["status"] == "done"


def test_match_solo_activity_with_multiple_falls_through():
    activities.create("correr", "física")
    activities.create("ler", "estudo")
    out = match("fiz")
    assert out is None
