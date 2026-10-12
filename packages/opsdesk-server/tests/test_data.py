import pytest

from opsdesk.data import UnknownServiceError, seed_store


def test_seed_store_has_three_services_and_incidents() -> None:
    store = seed_store()
    assert set(store.services) == {"api", "payments", "search"}
    assert [i.id for i in store.list_incidents("open")] == ["INC-101", "INC-102"]
    assert [i.id for i in store.list_incidents()] == ["INC-101", "INC-102", "INC-099"]


def test_restart_marks_service_healthy_and_counts() -> None:
    store = seed_store()
    svc = store.restart("search")
    assert (svc.state, svc.restarts) == ("healthy", 1)


def test_unknown_service_raises() -> None:
    with pytest.raises(UnknownServiceError):
        seed_store().service("nope")


def test_seed_store_returns_independent_copies() -> None:
    a, b = seed_store(), seed_store()
    a.restart("api")
    assert b.service("api").state == "degraded"
