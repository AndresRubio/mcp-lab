"""Deterministic in-memory seed data for the OpsDesk demo domain."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ServiceState = Literal["healthy", "degraded", "down"]
IncidentStatus = Literal["open", "resolved"]


class Service(BaseModel):
    name: str
    state: ServiceState
    restarts: int = 0


class Incident(BaseModel):
    id: str
    service: str
    title: str
    status: IncidentStatus
    description: str


class UnknownServiceError(LookupError):
    """Raised when a service name is not in the store."""


class OpsDeskStore:
    """Mutable in-memory state. Each test gets a fresh copy via `seed_store()`."""

    def __init__(self, services: list[Service], incidents: list[Incident], runbooks: dict[str, str]) -> None:
        self.services = {s.name: s for s in services}
        self.incidents = incidents
        self.runbooks = runbooks

    def service(self, name: str) -> Service:
        try:
            return self.services[name]
        except KeyError:
            raise UnknownServiceError(name) from None

    def restart(self, name: str) -> Service:
        svc = self.service(name)
        svc.state = "healthy"
        svc.restarts += 1
        return svc

    def list_incidents(self, status: IncidentStatus | None = None) -> list[Incident]:
        return [i for i in self.incidents if status is None or i.status == status]

    def incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self.incidents if i.id == incident_id), None)


def seed_store() -> OpsDeskStore:
    return OpsDeskStore(
        services=[
            Service(name="api", state="degraded"),
            Service(name="payments", state="healthy"),
            Service(name="search", state="down"),
        ],
        incidents=[
            Incident(
                id="INC-101",
                service="api",
                title="Elevated 5xx on /checkout",
                status="open",
                description="Reported by customer jane.doe@example.com: checkout fails intermittently.",
            ),
            Incident(
                id="INC-102",
                service="search",
                title="Search cluster unreachable",
                status="open",
                description="All search nodes failing health checks since 09:12 UTC.",
            ),
            Incident(
                id="INC-099",
                service="payments",
                title="Delayed payout batch",
                status="resolved",
                description="Batch delayed 40 minutes; contact ops-lead@example.com for details.",
            ),
        ],
        runbooks={
            "api": "# api runbook\n\n1. Check error rate dashboard.\n2. Restart if 5xx > 5% for 10 min.",
            "payments": "# payments runbook\n\n1. Never restart during payout window (00:00-02:00 UTC).",
            "search": "# search runbook\n\n1. Check node health.\n2. Restart the cluster.",
        },
    )
