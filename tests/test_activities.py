from datetime import date, datetime, timedelta

import pytest

from src import activities
from src.db import conn_ctx


def test_create_requires_name_and_category():
    with pytest.raises(ValueError):
        activities.create("", "física")
    with pytest.raises(ValueError):
        activities.create("correr", "")


def test_create_and_list():
    aid = activities.create("correr", "física")
    assert aid > 0
    rows = activities.list_active()
    assert len(rows) == 1
    assert rows[0]["name"] == "correr"
    assert rows[0]["category"] == "física"
    assert rows[0]["days_of_week"] is None


def test_create_with_days_of_week():
    aid = activities.create("ler", "estudo", days_of_week=[0, 2, 4])
    a = activities.get_by_id(aid)
    assert a["days_of_week"] == [0, 2, 4]


def test_get_by_name_case_insensitive():
    activities.create("Correr", "física")
    a = activities.get_by_name("correr")
    assert a is not None
    assert a["name"] == "Correr"


def test_list_active_filters_by_category():
    activities.create("correr", "física")
    activities.create("ler", "estudo")
    activities.create("alongar", "física")
    physical = activities.list_active(category="física")
    assert len(physical) == 2
    names = {r["name"] for r in physical}
    assert names == {"correr", "alongar"}


def test_list_due_today():
    today_idx = datetime.now().weekday()
    other_day = (today_idx + 1) % 7

    activities.create("hoje", "x", days_of_week=[today_idx])
    activities.create("outro_dia", "x", days_of_week=[other_day])
    activities.create("todo_dia", "x", days_of_week=None)

    due = activities.list_due_today()
    names = {a["name"] for a in due}
    assert "hoje" in names
    assert "todo_dia" in names
    assert "outro_dia" not in names


def test_track_creates_log():
    aid = activities.create("correr", "física")
    lid = activities.track(aid, "done")
    assert lid > 0
    log = activities.get_log(aid)
    assert log["status"] == "done"


def test_track_upsert_idempotent():
    aid = activities.create("correr", "física")
    activities.track(aid, "done")
    activities.track(aid, "done")  # 2x não duplica
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_logs WHERE activity_id = ?", (aid,)
        ).fetchall()
    assert len(rows) == 1


def test_track_upsert_changes_status():
    aid = activities.create("correr", "física")
    activities.track(aid, "done")
    activities.track(aid, "skipped")  # mesmo dia, status diferente
    log = activities.get_log(aid)
    assert log["status"] == "skipped"


def test_track_invalid_status():
    aid = activities.create("correr", "física")
    with pytest.raises(ValueError):
        activities.track(aid, "wrong_status")


def test_untrack_removes_log():
    aid = activities.create("correr", "física")
    activities.track(aid, "done")
    assert activities.untrack(aid) is True
    assert activities.get_log(aid) is None
    # Segunda chamada → False
    assert activities.untrack(aid) is False


def test_compliance_zero_when_no_logs():
    aid = activities.create("correr", "física")
    c = activities.compliance(aid, days=7)
    assert c["done"] == 0
    assert c["expected"] == 7
    assert c["percent"] == 0.0


def test_compliance_with_done_and_skipped():
    aid = activities.create("correr", "física")
    activities.track(aid, "done")
    # Inserir 'skipped' 2 dias atrás
    two_days_ago = (datetime.now().date() - timedelta(days=2)).isoformat()
    activities.track(aid, "skipped", log_date=two_days_ago)

    c = activities.compliance(aid, days=7)
    assert c["done"] == 1
    assert c["skipped"] == 1
    assert c["expected"] == 7
    assert c["percent"] == round(100 * 1 / 7, 1)


def test_compliance_respects_days_of_week():
    """Atividade só seg/qua/sex: expected na janela considera só esses dias."""
    aid = activities.create("ler", "estudo", days_of_week=[0, 2, 4])
    c = activities.compliance(aid, days=7)
    # Janela de 7 dias tem exatamente 3 dias previstos (segunda, quarta, sexta)
    assert c["expected"] == 3


def test_category_compliance_aggregates():
    a1 = activities.create("correr", "física")
    a2 = activities.create("alongar", "física")
    activities.create("ler", "estudo")  # outra categoria

    activities.track(a1, "done")
    activities.track(a2, "done")

    c = activities.category_compliance("física", days=1)
    assert c["category"] == "física"
    assert c["done"] == 2
    assert c["expected"] == 2
    assert c["percent"] == 100.0
    assert len(c["per_activity"]) == 2


def test_deactivate_hides_from_lists():
    aid = activities.create("correr", "física")
    assert activities.deactivate(aid) is True
    assert activities.list_active() == []
    assert activities.list_due_today() == []
    # Segunda chamada → False
    assert activities.deactivate(aid) is False


def test_find_solo_active():
    assert activities.find_solo_active() is None
    aid = activities.create("correr", "física")
    solo = activities.find_solo_active()
    assert solo is not None
    assert solo["id"] == aid
    activities.create("ler", "estudo")
    assert activities.find_solo_active() is None
