from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from queue_totem_core import QueueTicket, StationConfig

from .conftest import (
    MANAGE_HEADERS,
    READ_HEADERS,
    call_next,
    client_for,
    emit,
    make_config,
    make_station_config,
    move_station,
)


class TestSingleStationCompatibility:
    """Sem `stations`, a v0.3.0 precisa ser indistinguível da v0.2.0."""

    async def test_display_payload_has_no_stations_key(self, client):
        await emit(client, "AG")
        await call_next(client)
        body = (await client.get("/queue/display")).json()
        assert "stations" not in body
        assert set(body) == {"current", "recent"}

    async def test_ticket_has_no_station(self, client):
        ticket = await emit(client, "AG")
        assert ticket["station"] is None
        assert ticket["station_entered_at"] is None

    async def test_call_next_with_station_is_rejected(self, client):
        await emit(client, "AG")
        resp = await client.post(
            "/queue/tickets/next", params={"station": "recepcao"}, headers=MANAGE_HEADERS
        )
        assert resp.status_code == 400
        assert "estação única" in resp.json()["detail"]

    async def test_move_station_is_rejected(self, client):
        ticket = await emit(client, "AG")
        resp = await client.patch(
            f"/queue/tickets/{ticket['id']}/station",
            json={"station": "recepcao"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 400

    async def test_list_with_station_is_rejected(self, client):
        resp = await client.get(
            "/queue/tickets", params={"station": "recepcao"}, headers=READ_HEADERS
        )
        assert resp.status_code == 400


class TestEmissionWithStations:
    async def test_ticket_is_born_in_entry_station(self, station_client):
        ticket = await emit(station_client, "AG")
        assert ticket["station"] == "recepcao"
        assert ticket["station_label"] == "Recepção"
        assert ticket["station_entered_at"] is not None
        assert ticket["queued_since"] is not None


class TestCallNextPerStation:
    async def test_never_returns_ticket_from_another_station(
        self, station_client, session_factory
    ):
        a = await emit(station_client, "AG")
        b = await emit(station_client, "AG")
        await move_station(station_client, b["id"], "consultorio")

        # `a` continua na recepção; chamar no consultório só pode trazer `b`.
        assert (await call_next(station_client, station="consultorio"))["id"] == b["id"]
        assert (await call_next(station_client, station="recepcao"))["id"] == a["id"]

    async def test_empty_station_returns_404(self, station_client):
        await emit(station_client, "AG")  # nasce na recepção
        resp = await station_client.post(
            "/queue/tickets/next",
            params={"station": "farmacia"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 404

    async def test_two_stations_get_different_tickets(self, station_client):
        a = await emit(station_client, "AG")
        b = await emit(station_client, "AG")
        await move_station(station_client, b["id"], "consultorio")

        first = await call_next(station_client, station="recepcao")
        second = await call_next(station_client, station="consultorio")
        assert first["id"] != second["id"]
        assert {first["id"], second["id"]} == {a["id"], b["id"]}

    async def test_without_station_keeps_global_behaviour(self, station_client):
        a = await emit(station_client, "AG")
        b = await emit(station_client, "AG")
        await move_station(station_client, b["id"], "consultorio")
        # Sem o parâmetro, varre todas as estações — comportamento da v0.2.0.
        assert (await call_next(station_client))["id"] == a["id"]

    async def test_unknown_station_is_rejected(self, station_client):
        resp = await station_client.post(
            "/queue/tickets/next", params={"station": "lua"}, headers=MANAGE_HEADERS
        )
        assert resp.status_code == 400
        assert "Estação desconhecida" in resp.json()["detail"]


class TestMoveStation:
    async def test_requeues_and_preserves_queued_since(self, station_client):
        ticket = await emit(station_client, "AG")
        called = await call_next(station_client, station="recepcao")
        assert called["status"] == "chamado"

        moved = await move_station(station_client, ticket["id"], "consultorio")
        assert moved["station"] == "consultorio"
        assert moved["station_label"] == "Consultório"
        assert moved["status"] == "na_fila"
        assert moved["queued_since"] == ticket["queued_since"]
        assert datetime.fromisoformat(moved["station_entered_at"]) >= datetime.fromisoformat(
            ticket["station_entered_at"]
        )

    async def test_reports_time_spent_in_previous_station(self, station_client):
        ticket = await emit(station_client, "AG")
        moved = await move_station(station_client, ticket["id"], "consultorio")
        assert moved["previous_station"] == "recepcao"
        assert moved["previous_station_seconds"] >= 0

    async def test_clears_call_state(self, station_client):
        ticket = await emit(station_client, "AG")
        await call_next(station_client, station="recepcao")
        await station_client.patch(
            f"/queue/tickets/{ticket['id']}/recall", headers=MANAGE_HEADERS
        )
        moved = await move_station(station_client, ticket["id"], "consultorio")
        # Chega na nova fila zerado: não conta como chamado no painel nem
        # consome a rechamada da etapa anterior.
        assert moved["called_at"] is None
        assert moved["finished_at"] is None
        assert moved["recall_count"] == 0

    async def test_unknown_station_is_rejected(self, station_client):
        ticket = await emit(station_client, "AG")
        resp = await station_client.patch(
            f"/queue/tickets/{ticket['id']}/station",
            json={"station": "lua"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 400

    async def test_requires_manage_role(self, station_client):
        ticket = await emit(station_client, "AG")
        resp = await station_client.patch(
            f"/queue/tickets/{ticket['id']}/station", json={"station": "consultorio"}
        )
        assert resp.status_code == 403

    async def test_missing_ticket_returns_404(self, station_client):
        resp = await station_client.patch(
            "/queue/tickets/9999/station",
            json={"station": "consultorio"},
            headers=MANAGE_HEADERS,
        )
        assert resp.status_code == 404


class TestFifoUsesQueuedSince:
    async def test_earlier_arrival_wins_even_entering_station_later(
        self, station_client, session_factory
    ):
        first = await emit(station_client, "AG")
        second = await emit(station_client, "AG")

        # `first` entra no consultório antes de `second`...
        await move_station(station_client, first["id"], "consultorio")
        await move_station(station_client, second["id"], "consultorio")

        # ...mas `second` chegou à unidade bem antes (esperou mais na recepção).
        async with session_factory() as session:
            ticket = await session.get(QueueTicket, second["id"])
            ticket.queued_since = ticket.queued_since - timedelta(minutes=40)
            await session.commit()

        # A ordem da fila do consultório usa a chegada original, não o
        # station_entered_at nem o id.
        assert (await call_next(station_client, station="consultorio"))["id"] == second["id"]

    async def test_list_is_sorted_by_arrival(self, station_client, session_factory):
        first = await emit(station_client, "AG")
        second = await emit(station_client, "AG")
        async with session_factory() as session:
            ticket = await session.get(QueueTicket, second["id"])
            ticket.queued_since = ticket.queued_since - timedelta(minutes=40)
            await session.commit()

        resp = await station_client.get(
            "/queue/tickets", params={"station": "recepcao"}, headers=READ_HEADERS
        )
        assert [t["id"] for t in resp.json()] == [second["id"], first["id"]]


class TestPriorityInsideStation:
    async def test_priority_order_applies_within_the_station(self, station_client):
        normal = await emit(station_client, "AG")
        priority = await emit(station_client, "AG", is_priority=True)
        await move_station(station_client, normal["id"], "consultorio")
        await move_station(station_client, priority["id"], "consultorio")

        assert (await call_next(station_client, station="consultorio"))["id"] == priority["id"]

    async def test_priority_in_another_station_does_not_jump_the_queue(
        self, station_client
    ):
        normal = await emit(station_client, "AG")
        priority = await emit(station_client, "AG", is_priority=True)
        await move_station(station_client, priority["id"], "consultorio")

        # O prioritário está no consultório; a recepção chama o seu normal.
        assert (await call_next(station_client, station="recepcao"))["id"] == normal["id"]

    async def test_normals_per_priority_is_counted_per_station(self, session_factory):
        config = make_station_config(normals_per_priority=1)
        async with client_for(session_factory, config) as client:
            # Duas normais chamadas na recepção não podem consumir a cota do
            # consultório: lá a intercalação começa do zero.
            n1 = await emit(client, "AG")
            n2 = await emit(client, "AG")
            await call_next(client, station="recepcao")
            await call_next(client, station="recepcao")

            normal = await emit(client, "AG")
            priority = await emit(client, "AG", is_priority=True)
            await move_station(client, normal["id"], "consultorio")
            await move_station(client, priority["id"], "consultorio")

            # Nenhuma normal chamada no consultório ainda (streak 0 < 1), então a
            # intercalação manda chamar a normal primeiro. Se a contagem fosse
            # global, as duas normais da recepção já teriam estourado a cota e o
            # prioritário viria antes.
            assert (await call_next(client, station="consultorio"))["id"] == normal["id"]
            assert (await call_next(client, station="consultorio"))["id"] == priority["id"]
            assert {n1["id"], n2["id"]}.isdisjoint({priority["id"], normal["id"]})


class TestDisplayWithStations:
    async def test_one_current_per_station_plus_legacy_current(self, station_client):
        a = await emit(station_client, "AG")
        b = await emit(station_client, "AG")
        await move_station(station_client, b["id"], "consultorio")
        await call_next(station_client, station="recepcao")
        await call_next(station_client, station="consultorio")

        body = (await station_client.get("/queue/display")).json()
        by_station = {s["station"]: s for s in body["stations"]}
        assert set(by_station) == {"recepcao", "consultorio", "farmacia"}
        assert by_station["recepcao"]["current"]["id"] == a["id"]
        assert by_station["consultorio"]["current"]["id"] == b["id"]
        assert by_station["consultorio"]["station_label"] == "Consultório"
        assert by_station["farmacia"]["current"] is None
        # `current` legado: o chamado mais recente entre todas as estações.
        assert body["current"]["id"] == b["id"]

    async def test_empty_queue_lists_every_station(self, station_client):
        body = (await station_client.get("/queue/display")).json()
        assert body["current"] is None
        assert body["recent"] == []
        assert [s["current"] for s in body["stations"]] == [None, None, None]


class TestListFilteredByStation:
    async def test_filter_by_station(self, station_client):
        a = await emit(station_client, "AG")
        b = await emit(station_client, "AG")
        await move_station(station_client, b["id"], "farmacia")

        resp = await station_client.get(
            "/queue/tickets", params={"station": "farmacia"}, headers=READ_HEADERS
        )
        assert [t["id"] for t in resp.json()] == [b["id"]]

        resp = await station_client.get(
            "/queue/tickets", params={"station": "recepcao"}, headers=READ_HEADERS
        )
        assert [t["id"] for t in resp.json()] == [a["id"]]

    async def test_filter_combines_with_status(self, station_client):
        await emit(station_client, "AG")
        await emit(station_client, "AG")
        called = await call_next(station_client, station="recepcao")

        resp = await station_client.get(
            "/queue/tickets",
            params={"station": "recepcao", "status": "chamado"},
            headers=READ_HEADERS,
        )
        assert [t["id"] for t in resp.json()] == [called["id"]]


class TestStationConfigValidation:
    def test_entry_station_without_stations_is_rejected(self):
        with pytest.raises(ValidationError, match="entry_station foi informado"):
            make_config(entry_station="recepcao")

    def test_entry_station_must_exist(self):
        with pytest.raises(ValidationError, match="não está em stations"):
            make_station_config(entry_station="lua")

    def test_entry_station_is_required(self):
        with pytest.raises(ValidationError, match="entry_station é obrigatório"):
            make_config(stations=[StationConfig(code="a", label="A")])

    def test_duplicate_station_codes_rejected(self):
        with pytest.raises(ValidationError, match="códigos duplicados"):
            make_station_config(
                stations=[
                    StationConfig(code="a", label="A"),
                    StationConfig(code="a", label="Outro A"),
                ],
                entry_station="a",
            )

    def test_empty_station_list_rejected(self):
        with pytest.raises(ValidationError, match="lista vazia"):
            make_config(stations=[], entry_station="a")
