from datetime import datetime, timedelta

import pytest

from src.handlers.reminders import _parse_when


def test_parse_when_today_hh_mm():
    # 23:59 de hoje (sempre futuro a menos que rode no minuto exato — ok pra teste)
    future_time = (datetime.now() + timedelta(hours=1)).strftime("%H:%M")
    run_at, consumed = _parse_when(["hoje", future_time, "extra"])
    assert consumed == 2
    assert run_at > datetime.now()


def test_parse_when_tomorrow_hh_mm():
    run_at, consumed = _parse_when(["amanhã", "09:00", "tomar remédio"])
    assert consumed == 2
    expected = (datetime.now() + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    assert run_at == expected


def test_parse_when_iso_date_time():
    future = datetime.now() + timedelta(days=2)
    date_str = future.strftime("%Y-%m-%d")
    run_at, consumed = _parse_when([date_str, "10:00", "reuniao"])
    assert consumed == 2
    assert run_at.date() == future.date()
    assert run_at.hour == 10


def test_parse_when_invalid_raises():
    with pytest.raises(ValueError):
        _parse_when([])
    with pytest.raises(ValueError):
        _parse_when(["xyz"])


def test_parse_when_today_already_passed():
    # 00:01 já passou (a menos que rode entre 00:00-00:01)
    with pytest.raises(ValueError):
        _parse_when(["hoje", "00:01"])
