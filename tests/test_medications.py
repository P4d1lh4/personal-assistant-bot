from datetime import datetime, timedelta

from src import medications
from src.db import conn_ctx


def test_create_and_list():
    mid = medications.create("sertralina", "0 9 * * *")
    assert mid > 0
    rows = medications.list_active()
    assert len(rows) == 1
    assert rows[0]["name"] == "sertralina"


def test_create_empty_name_raises():
    import pytest
    with pytest.raises(ValueError):
        medications.create("", "0 9 * * *")
    with pytest.raises(ValueError):
        medications.create("X", "")


def test_get_by_name_case_insensitive():
    medications.create("Vitamina D", "0 8 * * *")
    m = medications.get_by_name("vitamina d")
    assert m is not None
    assert m["name"] == "Vitamina D"


def test_find_solo_active():
    assert medications.find_solo_active() is None
    mid = medications.create("creatina", "0 23 * * *")
    solo = medications.find_solo_active()
    assert solo is not None
    assert solo["id"] == mid
    medications.create("whey", "0 8 * * *")
    # Mais de um → solo retorna None
    assert medications.find_solo_active() is None


def test_track_creates_intake():
    mid = medications.create("creatina", "0 23 * * *")
    iid = medications.track(mid)
    assert iid > 0
    assert medications.has_intake_today(mid) is True


def test_track_skipped_does_not_count_as_taken():
    mid = medications.create("creatina", "0 23 * * *")
    medications.track(mid, skipped=True)
    assert medications.has_intake_today(mid) is False


def test_untrack_today_removes_only_taken():
    mid = medications.create("creatina", "0 23 * * *")
    medications.track(mid, skipped=False)
    medications.track(mid, skipped=True)
    removed = medications.untrack_today(mid)
    assert removed == 1
    # Skip permanece
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM medication_intakes WHERE medication_id = ?", (mid,)
        ).fetchall()
    assert len(rows) == 1


def test_compliance_zero_when_no_intakes():
    mid = medications.create("creatina", "0 23 * * *")
    result = medications.compliance(mid, days=7)
    assert result["taken"] == 0
    assert result["total"] == 7
    assert result["percent"] == 0


def test_compliance_with_intakes():
    mid = medications.create("creatina", "0 23 * * *")
    medications.track(mid)  # hoje
    # Inserir 2 dias atrás manualmente
    two_days_ago = (datetime.now() - timedelta(days=2)).isoformat()
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO medication_intakes (medication_id, taken_at, skipped) VALUES (?, ?, 0)",
            (mid, two_days_ago),
        )
    result = medications.compliance(mid, days=7)
    assert result["taken"] == 2
    assert result["total"] == 7
    assert result["percent"] == round(100 * 2 / 7, 1)


def test_deactivate():
    mid = medications.create("creatina", "0 23 * * *")
    assert medications.deactivate(mid) is True
    assert medications.list_active() == []
    # 2ª chamada não deve falhar
    assert medications.deactivate(mid) is False
