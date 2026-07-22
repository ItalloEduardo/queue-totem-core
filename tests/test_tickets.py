from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

import queue_totem_core.router as router_module
from queue_totem_core import QueueTicket, TicketTypeConfig

from .conftest import MANAGE_HEADERS, READ_HEADERS, call_next, emit, make_config


class TestEmission:
    async def test_sequences_are_per_type(self, client):
        assert (await emit(client, "AG"))["ticket_number"] == "AG-001"
        assert (await emit(client, "AG"))["ticket_number"] == "AG-002"
        assert (await emit(client, "TR"))["ticket_number"] == "TR-001"
        assert (await emit(client, "FM"))["ticket_number"] == "FM-001"
        assert (await emit(client, "AG"))["ticket_number"] == "AG-003"

    async def test_ticket_is_born_in_queue_with_label(self, client):
        ticket = await emit(client, "AG", reference_code="PROTO-123", reference_label="Rex")
        assert ticket["status"] == "na_fila"
        assert ticket["ticket_label"] == "Agendamento"
        assert ticket["reference_code"] == "PROTO-123"
        assert ticket["reference_label"] == "Rex"

    async def test_unknown_type_is_rejected(self, client):
        resp = await client.post("/queue/tickets", json={"ticket_type": "XX"})
        assert resp.status_code == 400

    async def test_priority_rejected_for_none_source(self, client):
        resp = await client.post(
            "/queue/tickets", json={"ticket_type": "FM", "is_priority": True}
        )
        assert resp.status_code == 400

    async def test_priority_reason_stored_only_when_priority(self, client):
        ticket = await emit(client, "TR", is_priority=True, priority_reason="Idoso")
        assert ticket["is_priority"] is True
        assert ticket["priority_reason"] == "Idoso"
        ticket = await emit(client, "TR", is_priority=False, priority_reason="ignorar")
        assert ticket["priority_reason"] is None

    async def test_sequence_retries_on_integrity_conflict(self, client, monkeypatch):
        await emit(client, "AG")  # ocupa a sequência 1
        real_count = router_module._count_existing
        calls = {"n": 0}

        async def stale_then_real(db, ticket_type, ticket_date, daily_reset):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0  # valor obsoleto -> tenta sequência 1 de novo -> IntegrityError
            return await real_count(db, ticket_type, ticket_date, daily_reset)

        monkeypatch.setattr(router_module, "_count_existing", stale_then_real)
        ticket = await emit(client, "AG")
        assert ticket["ticket_number"] == "AG-002"
        assert calls["n"] == 2

    async def test_sequence_gives_up_after_max_attempts(self, client, monkeypatch):
        await emit(client, "AG")

        async def always_stale(db, ticket_type, ticket_date, daily_reset):
            return 0

        monkeypatch.setattr(router_module, "_count_existing", always_stale)
        resp = await client.post("/queue/tickets", json={"ticket_type": "AG"})
        assert resp.status_code == 409

    async def test_daily_reset_ignores_previous_days(self, session_factory, client):
        yesterday = date.today() - timedelta(days=1)
        async with session_factory() as session:
            session.add(
                QueueTicket(
                    ticket_type="AG",
                    ticket_date=yesterday,
                    sequence=1,
                    ticket_number="AG-001",
                )
            )
            await session.commit()
        # daily_reset=True (default do app fixture): ontem não conta
        ticket = await emit(client, "AG")
        assert ticket["ticket_number"] == "AG-001"

    async def test_no_daily_reset_counts_across_days(self, session_factory):
        yesterday = date.today() - timedelta(days=1)
        async with session_factory() as session:
            session.add(
                QueueTicket(
                    ticket_type="AG",
                    ticket_date=yesterday,
                    sequence=1,
                    ticket_number="AG-001",
                )
            )
            await session.commit()

        from httpx import ASGITransport, AsyncClient

        from .conftest import make_app

        app = make_app(session_factory, make_config(daily_reset=False))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ticket = await emit(client, "AG")
        assert ticket["ticket_number"] == "AG-002"


class TestPriorityOrder:
    async def test_call_next_follows_rank_then_fifo(self, client):
        fm = await emit(client, "FM")
        tr_normal_1 = await emit(client, "TR")
        tr_pri = await emit(client, "TR", is_priority=True)
        ag_normal = await emit(client, "AG")
        ag_pri = await emit(client, "AG", is_priority=True)
        tr_normal_2 = await emit(client, "TR")

        expected = [ag_pri, ag_normal, tr_pri, tr_normal_1, tr_normal_2, fm]
        for exp in expected:
            called = await call_next(client)
            assert called["id"] == exp["id"], (
                f"esperado {exp['ticket_number']}, veio {called['ticket_number']}"
            )
            assert called["status"] == "chamado"
            assert called["called_at"] is not None

    async def test_call_next_on_empty_queue_returns_404(self, client):
        resp = await client.post("/queue/tickets/next", headers=MANAGE_HEADERS)
        assert resp.status_code == 404

    async def test_list_is_sorted_by_priority(self, client):
        await emit(client, "FM")
        await emit(client, "TR", is_priority=True)
        await emit(client, "AG")
        resp = await client.get("/queue/tickets", headers=READ_HEADERS)
        assert resp.status_code == 200
        numbers = [t["ticket_number"] for t in resp.json()]
        assert numbers == ["AG-001", "TR-001", "FM-001"]


class TestLifecycle:
    async def test_recall_respects_limit(self, client):
        ticket = await emit(client, "AG")
        called = await call_next(client)
        assert called["id"] == ticket["id"]

        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/recall", headers=MANAGE_HEADERS
        )
        assert resp.status_code == 200
        assert resp.json()["recall_count"] == 1

        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/recall", headers=MANAGE_HEADERS
        )
        assert resp.status_code == 400
        assert "não compareceu" in resp.json()["detail"]

    async def test_recall_requires_called_status(self, client):
        ticket = await emit(client, "AG")
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/recall", headers=MANAGE_HEADERS
        )
        assert resp.status_code == 400

    async def test_no_show_only_from_called(self, client):
        ticket = await emit(client, "AG")
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/no-show", headers=MANAGE_HEADERS
        )
        assert resp.status_code == 400

        await call_next(client)
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/no-show", headers=MANAGE_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "nao_compareceu"
        assert body["finished_at"] is not None

    async def test_happy_path_transitions(self, client):
        ticket = await emit(client, "AG")
        await call_next(client)

        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/status",
            json={"status": "em_atendimento"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "em_atendimento"

        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/status",
            json={"status": "concluido"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "concluido"
        assert resp.json()["finished_at"] is not None

    async def test_invalid_transitions_are_rejected(self, client):
        ticket = await emit(client, "AG")
        # na_fila -> em_atendimento (sem passar por chamado)
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/status",
            json={"status": "em_atendimento"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 400
        # na_fila -> concluido
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/status",
            json={"status": "concluido"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 400
        # status fora do enum
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/status",
            json={"status": "nao_compareceu"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 422

    async def test_ticket_not_found(self, client):
        resp = await client.patch("/queue/tickets/9999/recall", headers=MANAGE_HEADERS)
        assert resp.status_code == 404


class TestDisplay:
    async def test_display_shows_current_and_recent(self, client):
        t1 = await emit(client, "AG")
        t2 = await emit(client, "AG")
        await call_next(client)  # chama t1
        resp = await client.patch(
            f"/queue/tickets/{t1['id']}/status",
            json={"status": "em_atendimento"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 200
        await call_next(client)  # chama t2

        resp = await client.get("/queue/display")
        assert resp.status_code == 200
        body = resp.json()
        assert body["current"]["id"] == t2["id"]
        recent_ids = [t["id"] for t in body["recent"]]
        assert recent_ids == [t2["id"], t1["id"]]

    async def test_display_empty_queue(self, client):
        resp = await client.get("/queue/display")
        assert resp.status_code == 200
        assert resp.json() == {"current": None, "recent": []}

    async def test_display_respects_limit(self, client):
        for _ in range(4):
            await emit(client, "FM")
            await call_next(client)
        resp = await client.get("/queue/display", params={"limit": 2})
        assert len(resp.json()["recent"]) == 2


class TestAuth:
    async def test_public_endpoints_need_no_auth(self, client):
        assert (await client.post("/queue/tickets", json={"ticket_type": "AG"})).status_code == 201
        assert (await client.get("/queue/display")).status_code == 200

    async def test_read_endpoint_requires_auth(self, client):
        assert (await client.get("/queue/tickets")).status_code == 401

    async def test_manage_endpoints_require_role(self, client):
        ticket = await emit(client, "AG")
        assert (await client.post("/queue/tickets/next")).status_code == 403
        assert (
            await client.post("/queue/tickets/next", headers=READ_HEADERS)
        ).status_code == 403
        for path in ("recall", "no-show"):
            resp = await client.patch(f"/queue/tickets/{ticket['id']}/{path}")
            assert resp.status_code == 403


class TestConfigValidation:
    def test_priority_order_must_cover_all_combinations(self):
        with pytest.raises(ValidationError, match="não cobre"):
            make_config(priority_order=[("AG", True), ("TR", True), ("TR", False), ("FM", False)])

    def test_priority_order_rejects_unknown_type(self):
        with pytest.raises(ValidationError, match="desconhecido"):
            make_config(
                priority_order=[
                    ("AG", True),
                    ("AG", False),
                    ("TR", True),
                    ("TR", False),
                    ("FM", False),
                    ("ZZ", False),
                ]
            )

    def test_priority_order_rejects_priority_for_none_source(self):
        with pytest.raises(ValidationError, match="nunca é prioritário"):
            make_config(
                priority_order=[
                    ("AG", True),
                    ("AG", False),
                    ("TR", True),
                    ("TR", False),
                    ("FM", True),
                    ("FM", False),
                ]
            )

    def test_duplicate_type_codes_rejected(self):
        with pytest.raises(ValidationError, match="duplicados"):
            make_config(
                ticket_types=[
                    TicketTypeConfig(code="AG", label="A", priority_source="none"),
                    TicketTypeConfig(code="AG", label="B", priority_source="none"),
                ],
                priority_order=[("AG", False)],
            )

    def test_invalid_timezone_rejected(self):
        with pytest.raises(ValidationError):
            make_config(timezone="Marte/Cratera")
