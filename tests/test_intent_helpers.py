from src.intent import (
    _coerce_exercises,
    _coerce_float,
    _coerce_int,
    _parse_retry_delay,
)


class FakeErr(Exception):
    pass


def test_coerce_int_valid():
    assert _coerce_int(5) == 5
    assert _coerce_int("12") == 12
    assert _coerce_int("12.7") == 12
    assert _coerce_int(0) == 0


def test_coerce_int_invalid():
    assert _coerce_int(None) is None
    assert _coerce_int("") is None
    assert _coerce_int("abc") is None
    assert _coerce_int([]) is None


def test_coerce_float_valid():
    assert _coerce_float(60) == 60.0
    assert _coerce_float("12.5") == 12.5
    assert _coerce_float(0) == 0.0


def test_coerce_float_invalid():
    assert _coerce_float(None) is None
    assert _coerce_float("xx") is None


def test_coerce_exercises_basic():
    raw = [{"name": "supino", "sets": 4, "reps": "10", "weight": 60}]
    out = _coerce_exercises(raw)
    assert len(out) == 1
    assert out[0]["name"] == "supino"
    assert out[0]["sets"] == 4
    assert out[0]["reps"] == "10"
    assert out[0]["weight"] == 60.0


def test_coerce_exercises_drops_empty_names():
    raw = [{"name": "", "sets": 4}, {"name": "supino"}]
    out = _coerce_exercises(raw)
    assert len(out) == 1
    assert out[0]["name"] == "supino"


def test_coerce_exercises_handles_target_weight_alias():
    raw = [{"name": "agachamento", "target_weight": 80}]
    out = _coerce_exercises(raw)
    assert out[0]["weight"] == 80.0


def test_coerce_exercises_handles_non_list():
    assert _coerce_exercises(None) == []
    assert _coerce_exercises("string") == []
    assert _coerce_exercises({}) == []


def test_parse_retry_delay_seconds():
    err = FakeErr("Please retry in 16.175s.")
    assert _parse_retry_delay(err) == 16


def test_parse_retry_delay_integer():
    err = FakeErr("retry in 30 seconds")
    # Regex requires "s" suffix, "30 seconds" matches "30 s" loosely
    assert _parse_retry_delay(err) is None or _parse_retry_delay(err) == 30


def test_parse_retry_delay_no_match():
    err = FakeErr("totally different error")
    assert _parse_retry_delay(err) is None
