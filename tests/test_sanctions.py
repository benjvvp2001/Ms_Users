"""HU-45: suspender o sancionar cuentas (repositorio simulado, sin base de datos)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.services.sanction_service as sanction_module
from test_admin_audit import AUDIT, USERS, file_report, items, person, setup

SANCTIONS = f"{USERS}/admin/sanctions"
STATUS = "/internal/v1/accounts/{}/status"
SERVICE_TOKEN = "service-to-service-secret-with-32-bytes-min"
PASSWORD = "A-secure-test-password-1"


@pytest.fixture
def clock(monkeypatch):
    """The sanctions clock; tests move it to let a temporal sanction expire."""
    state = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(sanction_module, "utcnow", lambda: state["now"])

    class Clock:
        def advance(self, **delta):
            state["now"] += timedelta(**delta)

        @property
        def now(self):
            return state["now"]
    return Clock()


def sanction(client, admin, target, tipo="temporal", dias=3, motivo="Acoso reiterado a otros deportistas", **extra):
    body = {"usuario_id": target["id"], "tipo": tipo, "motivo": motivo, **extra}
    if tipo == "temporal":
        body["dias"] = dias
    return client.post(SANCTIONS, headers=admin["headers"], json=body)


def lift(client, admin, sanction_id, motivo="Cumplió el compromiso"):
    return client.post(f"{SANCTIONS}/{sanction_id}/lift", headers=admin["headers"], json={"motivo": motivo})


def profile(client, who):
    return client.get(f"{USERS}/{who['id']}/profile", headers=who["headers"])


def login(client, email, password=PASSWORD):
    return client.post(f"{USERS}/auth/login", json={"email": email, "password": password})


def world():
    client, repository, admin, ana = setup()
    return client, repository, admin, ana, person(client, "Beto")


# -- aplicar -------------------------------------------------------------------------------------

def test_a_temporal_sanction_restricts_the_account_at_once_and_is_audited_and_notified(clock) -> None:
    client, repository, admin, ana, beto = world()
    assert profile(client, ana).status_code == 200
    response = sanction(client, admin, ana, dias=3)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["tipo"], body["estado"], body["reporte_id"]) == ("temporal", "activa", None)
    assert datetime.fromisoformat(body["fin"]) - datetime.fromisoformat(body["inicio"]) == timedelta(days=3)
    assert body["usuario"]["nombre"] == "Ana" and body["admin"]["user_id"] == admin["id"]

    blocked = profile(client, ana)                                   # The token ana already had.
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "account_suspended"
    assert blocked.json()["motivo"] == "Acoso reiterado a otros deportistas" and blocked.json()["tipo"] == "temporal"
    assert blocked.json()["fin"] == body["fin"]
    assert "Acoso reiterado" in blocked.json()["detail"] and "hasta el" in blocked.json()["detail"]
    assert profile(client, beto).status_code == 200                  # Somebody else is not affected.
    assert profile(client, admin).status_code == 200

    [event] = items(client, admin, "?accion=SANCTION_APPLIED")
    assert (event["motivo"], event["recurso_tipo"], event["recurso_id"]) == (
        "Acoso reiterado a otros deportistas", "user", ana["id"])
    assert event["afectado"]["nombre"] == "Ana T." and event["admin"]["nombre"] == "Moderadora T."
    assert event["detalle"] == {"sancion_id": body["id"], "tipo": "temporal", "dias": 3, "reporte_id": None}

    [notice] = repository.notifications
    assert notice["usuario_id"].hex == ana["id"].replace("-", "") and notice["tipo"] == "sancion"
    assert "Acoso reiterado" in notice["mensaje"] and "hasta el" in notice["mensaje"]
    [mail] = [m for m in client.app.state.email_sender.sent if "suspendida" in m["subject"]]
    assert mail["to"] == "ana@example.com" and "Acoso reiterado" in mail["text"]


def test_a_permanent_sanction_has_no_end_and_says_so() -> None:
    client, _, admin, ana, _ = world()
    body = sanction(client, admin, ana, tipo="permanente").json()
    assert body["tipo"] == "permanente" and body["fin"] is None and body["estado"] == "activa"
    blocked = profile(client, ana).json()
    assert blocked["code"] == "account_suspended" and blocked["fin"] is None and "permanente" in blocked["detail"]
    assert items(client, admin, "?accion=SANCTION_APPLIED")[0]["detalle"]["dias"] is None


def test_the_sanctioned_person_never_sees_who_reported_or_who_sanctioned(clock) -> None:
    client, repository, admin, ana, beto = world()
    report_id = file_report(client, beto, ana["id"], motivo="Acoso o trato irrespetuoso")
    client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"], json={"estado": "escalado"})
    sanction(client, admin, ana, reporte_id=report_id)
    for response in (profile(client, ana), login(client, "ana@example.com")):
        assert response.status_code == 403, response.text
        for private in (beto["id"], admin["id"], "Beto", "Moderadora", report_id, "admin"):
            assert private not in response.text, private
        assert set(response.json()) == {"detail", "code", "tipo", "motivo", "fin"}
    [notice] = repository.notifications
    assert beto["id"] not in json.dumps(notice, default=str) and "Beto" not in json.dumps(notice, default=str)


def test_login_is_blocked_for_the_sanctioned_account_only(clock) -> None:
    client, _, admin, ana, beto = world()
    sanction(client, admin, ana)
    blocked = login(client, "ana@example.com")
    assert blocked.status_code == 403 and blocked.json()["code"] == "account_suspended"
    assert login(client, "ana@example.com", "otra-clave-Incorrecta-1").status_code == 401   # Nothing revealed.
    assert login(client, "beto@example.com").status_code == 200
    assert login(client, "nadie@example.com").status_code == 401


def test_a_temporal_sanction_expires_by_itself(clock) -> None:
    client, _, admin, ana, _ = world()
    created = sanction(client, admin, ana, dias=2).json()
    clock.advance(days=1, hours=23)
    assert profile(client, ana).status_code == 403
    clock.advance(hours=2)                                           # Past `fin`; no job ran.
    assert profile(client, ana).status_code == 200
    assert login(client, "ana@example.com").status_code == 200
    assert client.get(f"{SANCTIONS}/{created['id']}", headers=admin["headers"]).json()["estado"] == "vencida"
    assert [s["id"] for s in client.get(f"{SANCTIONS}?estado=vencida", headers=admin["headers"]).json()] == [created["id"]]
    assert client.get(f"{SANCTIONS}?estado=activa", headers=admin["headers"]).json() == []
    # An ended one does not stand in the way of a new sanction.
    again = sanction(client, admin, ana, dias=5)
    assert again.status_code == 201 and profile(client, ana).status_code == 403


def test_only_one_sanction_in_force_per_account(clock) -> None:
    client, _, admin, ana, _ = world()
    first = sanction(client, admin, ana).json()
    assert sanction(client, admin, ana, tipo="permanente").status_code == 409
    assert len(client.get(f"{SANCTIONS}?usuario_id={ana['id']}", headers=admin["headers"]).json()) == 1
    assert lift(client, admin, first["id"]).status_code == 200
    assert sanction(client, admin, ana, tipo="permanente").status_code == 201


def test_nobody_sanctions_themselves_or_another_admin() -> None:
    client, repository, admin, ana, _ = world()
    other = person(client, "Segunda", "admin", repository)
    for target in (admin, other):
        response = sanction(client, admin, target)
        assert response.status_code == 422, response.text
    assert sanction(client, admin, {"id": str(uuid4())}).status_code == 404
    assert client.get(SANCTIONS, headers=admin["headers"]).json() == []


# -- desde un reporte ---------------------------------------------------------------------------

def test_a_sanction_can_come_from_an_escalated_report_of_that_account(clock) -> None:
    client, _, admin, ana, beto = world()
    report_id = file_report(client, beto, ana["id"])
    client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"], json={"estado": "escalado"})
    response = sanction(client, admin, ana, reporte_id=report_id)
    assert response.status_code == 201 and response.json()["reporte_id"] == report_id
    assert items(client, admin, "?accion=SANCTION_APPLIED")[0]["detalle"]["reporte_id"] == report_id


@pytest.mark.parametrize("state,expected", [(None, 409), ("revisado", 409), ("descartado", 409)])
def test_a_report_that_is_not_escalated_is_refused(state, expected) -> None:
    client, _, admin, ana, beto = world()
    report_id = file_report(client, beto, ana["id"])
    if state:
        client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"], json={"estado": state})
    assert sanction(client, admin, ana, reporte_id=report_id).status_code == expected
    assert profile(client, ana).status_code == 200 and client.get(SANCTIONS, headers=admin["headers"]).json() == []


def test_a_report_about_someone_else_or_missing_or_not_about_a_person_is_refused() -> None:
    client, _, admin, ana, beto = world()
    about_beto = file_report(client, ana, beto["id"])
    about_activity = client.post(f"{USERS}/reports", headers=beto["headers"], json={
        "tipo": "actividad", "objetivo_id": ana["id"], "objetivo_nombre": "Trote", "motivo": "Lugar inseguro",
        "detalle": "El punto de encuentro es peligroso"}).json()["id"]
    for report_id in (about_beto, about_activity):
        client.patch(f"{USERS}/admin/reports/{report_id}", headers=admin["headers"], json={"estado": "escalado"})
    assert sanction(client, admin, ana, reporte_id=about_beto).status_code == 422       # It is about Beto.
    assert sanction(client, admin, ana, reporte_id=about_activity).status_code == 422   # Not about a person.
    assert sanction(client, admin, ana, reporte_id=str(uuid4())).status_code == 422
    assert profile(client, ana).status_code == 200


# -- levantar ------------------------------------------------------------------------------------

def test_lifting_restores_access_audits_and_notifies(clock) -> None:
    client, repository, admin, ana, _ = world()
    created = sanction(client, admin, ana, tipo="permanente").json()
    assert profile(client, ana).status_code == 403
    lifted = lift(client, admin, created["id"], "Apelación aceptada")
    assert lifted.status_code == 200, lifted.text
    body = lifted.json()
    assert body["estado"] == "levantada" and body["motivo_levantamiento"] == "Apelación aceptada"
    assert body["levantada_por"]["user_id"] == admin["id"] and body["levantada_en"]
    assert profile(client, ana).status_code == 200 and login(client, "ana@example.com").status_code == 200

    [event] = items(client, admin, "?accion=SANCTION_LIFTED")
    assert (event["motivo"], event["recurso_id"], event["detalle"]["sancion_id"]) == ("Apelación aceptada", ana["id"], created["id"])
    assert [n["data"]["evento"] for n in repository.notifications] == ["aplicada", "levantada"]
    assert any("levantó" in m["subject"] for m in client.app.state.email_sender.sent)


def test_lifting_twice_is_idempotent_and_does_not_audit_or_notify_again(clock) -> None:
    client, repository, admin, ana, _ = world()
    created = sanction(client, admin, ana).json()
    first, again = lift(client, admin, created["id"]), lift(client, admin, created["id"], "Otro motivo distinto")
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()                                       # The first motivo stays.
    assert len(items(client, admin, "?accion=SANCTION_LIFTED")) == 1 and len(repository.notifications) == 2


def test_lifting_one_that_already_ended_changes_nothing(clock) -> None:
    client, _, admin, ana, _ = world()
    created = sanction(client, admin, ana, dias=1).json()
    clock.advance(days=2)
    response = lift(client, admin, created["id"])
    assert response.status_code == 200 and response.json()["estado"] == "vencida"
    assert items(client, admin, "?accion=SANCTION_LIFTED") == []


# -- permisos, 404 y datos invalidos ---------------------------------------------------------------

def test_401_without_token_and_403_for_player_and_club_admin_even_for_unknown_ids() -> None:
    client, repository, admin, ana, beto = world()
    club = person(client, "Rocio", "club_admin", repository)
    created = sanction(client, admin, beto).json()
    calls = [("post", SANCTIONS, {"usuario_id": ana["id"], "tipo": "permanente", "motivo": "Motivo válido"}),
             ("get", SANCTIONS, None), ("get", f"{SANCTIONS}/{created['id']}", None),
             ("post", f"{SANCTIONS}/{created['id']}/lift", {"motivo": "Motivo válido"}),
             ("post", f"{SANCTIONS}/{uuid4()}/lift", {"motivo": "Motivo válido"}),
             ("get", f"{SANCTIONS}/{uuid4()}", None)]
    for method, path, body in calls:
        assert getattr(client, method)(path, **({"json": body} if body else {})).status_code == 401, path
        for who in (ana, club):
            response = getattr(client, method)(path, headers=who["headers"], **({"json": body} if body else {}))
            assert response.status_code == 403, (path, who["id"])
    assert profile(client, ana).status_code == 200


def test_unknown_sanctions_are_404_for_admins() -> None:
    client, _, admin, _, _ = world()
    assert client.get(f"{SANCTIONS}/{uuid4()}", headers=admin["headers"]).status_code == 404
    assert lift(client, admin, uuid4()).status_code == 404


@pytest.mark.parametrize("body", [
    {"tipo": "temporal", "motivo": "Motivo válido"},                          # temporal without days
    {"tipo": "permanente", "dias": 5, "motivo": "Motivo válido"},             # permanent with days
    {"tipo": "temporal", "dias": 0, "motivo": "Motivo válido"}, {"tipo": "temporal", "dias": 366, "motivo": "Motivo válido"},
    {"tipo": "temporal", "dias": "5", "motivo": "Motivo válido"}, {"tipo": "temporal", "dias": True, "motivo": "Motivo válido"},
    {"tipo": "temporal", "dias": 1.5, "motivo": "Motivo válido"}, {"tipo": "temporal", "dias": float("nan"), "motivo": "Motivo válido"},
    {"tipo": "temporal", "dias": float("inf"), "motivo": "Motivo válido"},
    {"tipo": "temporal", "dias": 3, "motivo": "a\x00b contenido"}, {"tipo": "temporal", "dias": 3, "motivo": "\ud800 roto"},
    {"tipo": "temporal", "dias": 3, "motivo": "escribió a ana@example.com"}, {"tipo": "temporal", "dias": 3, "motivo": "ab"},
    {"tipo": "temporal", "dias": 3, "motivo": "x" * 301}, {"tipo": "temporal", "dias": 3, "motivo": 5},
    {"tipo": "temporal", "dias": 3}, {"tipo": "otra", "dias": 3, "motivo": "Motivo válido"},
    {"tipo": "temporal", "dias": 3, "motivo": "Motivo válido", "admin_id": "x"},
    {"tipo": "temporal", "dias": 3, "motivo": "Motivo válido", "reporte_id": "no-uuid-secreto"},
])
def test_invalid_bodies_are_422_without_echo_and_save_nothing(body) -> None:
    client, _, admin, ana, _ = world()
    response = client.post(SANCTIONS, headers={**admin["headers"], "Content-Type": "application/json"},
                           content=json.dumps({"usuario_id": ana["id"], **body}, allow_nan=True).encode())
    assert response.status_code == 422, response.text
    for leaked in ("input", "roto", "example.com", "secreto"):
        assert leaked not in response.text
    assert client.get(SANCTIONS, headers=admin["headers"]).json() == [] and profile(client, ana).status_code == 200


def test_invalid_ids_filters_and_lift_bodies_are_422() -> None:
    client, _, admin, ana, _ = world()
    created = sanction(client, admin, ana).json()
    assert client.get(f"{SANCTIONS}/no-es-uuid", headers=admin["headers"]).status_code == 422
    for query in ("estado=otro", "limit=0", "limit=101", "pagina=0", "usuario_id=x", "extra=1"):
        assert client.get(f"{SANCTIONS}?{query}", headers=admin["headers"]).status_code == 422, query
    for body in ({}, {"motivo": "ab"}, {"motivo": "ver ana@example.com"}, {"motivo": "bien", "x": 1}, {"motivo": "a\x00bcd"}):
        assert client.post(f"{SANCTIONS}/{created['id']}/lift", headers=admin["headers"], json=body).status_code == 422
    assert client.post(SANCTIONS, headers=admin["headers"], json={"usuario_id": "x"}).status_code == 422


# -- listado -------------------------------------------------------------------------------------

def test_listing_filters_by_state_and_user_and_pages_with_a_cap(clock) -> None:
    client, repository, admin, ana, beto = world()
    carla = person(client, "Carla")
    first = sanction(client, admin, ana).json()
    second = sanction(client, admin, beto, tipo="permanente").json()
    third = sanction(client, admin, carla, dias=1).json()
    lift(client, admin, first["id"])
    clock.advance(days=2)
    ids = lambda query: [s["id"] for s in client.get(f"{SANCTIONS}{query}", headers=admin["headers"]).json()]
    assert ids("") == [third["id"], second["id"], first["id"]]
    assert ids("?estado=activa") == [second["id"]]
    assert ids("?estado=levantada") == [first["id"]]
    assert ids("?estado=vencida") == [third["id"]]
    assert ids(f"?usuario_id={beto['id']}") == [second["id"]]
    assert ids("?limit=2") == [third["id"], second["id"]] and ids("?limit=2&pagina=2") == [first["id"]]
    text = client.get(SANCTIONS, headers=admin["headers"]).text
    assert "example.com" not in text and "password" not in text.lower()


# -- fallos --------------------------------------------------------------------------------------

def test_if_the_audit_fails_the_sanction_is_not_saved_and_the_answer_is_503(clock) -> None:
    client, repository, admin, ana, _ = world()
    repository.audit_unavailable = True
    assert sanction(client, admin, ana).status_code == 503
    repository.audit_unavailable = False
    assert profile(client, ana).status_code == 200 and client.get(SANCTIONS, headers=admin["headers"]).json() == []
    assert repository.notifications == []
    created = sanction(client, admin, ana).json()
    repository.audit_unavailable = True
    assert lift(client, admin, created["id"]).status_code == 503
    repository.audit_unavailable = False
    assert profile(client, ana).status_code == 403                          # Still suspended.


def test_a_failing_notice_or_email_never_undoes_the_sanction(clock, caplog) -> None:
    client, repository, admin, ana, _ = world()
    repository.notifications_unavailable = True
    client.app.state.email_sender.send = lambda **_: (_ for _ in ()).throw(RuntimeError("smtp down"))
    with caplog.at_level("ERROR"):
        response = sanction(client, admin, ana)
    assert response.status_code == 201 and profile(client, ana).status_code == 403
    assert "notice could not be saved" in caplog.text and "email could not be delivered" in caplog.text   # Not hidden.
    assert len(items(client, admin, "?accion=SANCTION_APPLIED")) == 1


# -- endpoint interno -----------------------------------------------------------------------------

def test_the_internal_status_endpoint_needs_the_service_secret_and_shows_only_the_restriction(monkeypatch, clock) -> None:
    client, _, admin, ana, beto = world()
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    headers = {"X-Internal-Service-Token": SERVICE_TOKEN}
    created = sanction(client, admin, ana, dias=2, motivo="Lenguaje ofensivo reiterado").json()

    assert client.get(STATUS.format(ana["id"])).status_code == 401
    assert client.get(STATUS.format(ana["id"]), headers={"X-Internal-Service-Token": "wrong"}).status_code == 401
    assert client.get(STATUS.format(ana["id"]), headers=admin["headers"]).status_code == 401   # A user token is not enough.
    assert client.get(STATUS.format(ana["id"]), headers=ana["headers"]).status_code == 401

    suspended = client.get(STATUS.format(ana["id"]), headers=headers)
    assert suspended.status_code == 200
    assert suspended.json() == {"suspended": True, "tipo": "temporal", "motivo": "Lenguaje ofensivo reiterado", "fin": created["fin"]}
    assert client.get(STATUS.format(beto["id"]), headers=headers).json() == {
        "suspended": False, "tipo": None, "motivo": None, "fin": None}
    assert client.get(STATUS.format(uuid4()), headers=headers).json()["suspended"] is False
    clock.advance(days=3)
    assert client.get(STATUS.format(ana["id"]), headers=headers).json()["suspended"] is False
    assert client.get(STATUS.format("no-uuid"), headers=headers).status_code == 422


def test_the_internal_status_endpoint_is_closed_without_a_configured_secret(monkeypatch) -> None:
    client, _, _, ana, _ = world()
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    assert client.get(STATUS.format(ana["id"]), headers={"X-Internal-Service-Token": SERVICE_TOKEN}).status_code == 503


def test_there_are_no_delete_or_edit_routes_for_sanctions() -> None:
    client, _, admin, ana, _ = world()
    created = sanction(client, admin, ana).json()
    for method in ("put", "patch", "delete"):
        for path in (SANCTIONS, f"{SANCTIONS}/{created['id']}", f"{SANCTIONS}/{created['id']}/lift"):
            assert getattr(client, method)(path, headers=admin["headers"]).status_code in (404, 405), (method, path)


# -- una cuenta suspendida no se ofrece a nadie ---------------------------------------------------


def ids_of(response):
    assert response.status_code == 200, response.text
    return {card["user_id"] for card in response.json()}


def test_a_suspended_account_leaves_the_suggestions_and_the_athlete_cards(clock):
    client, _, admin, ana, beto = world()
    cards = lambda: client.post(f"{USERS}/athletes/cards", headers=ana["headers"], json=[beto["id"]])  # noqa: E731
    assert beto["id"] in ids_of(client.get(f"{USERS}/suggestions", headers=ana["headers"]))
    assert ids_of(cards()) == {beto["id"]}
    created = sanction(client, admin, beto, tipo="temporal", dias=3)
    assert created.status_code == 201, created.text
    assert beto["id"] not in ids_of(client.get(f"{USERS}/suggestions", headers=ana["headers"]))
    assert ids_of(cards()) == set()
    assert client.get(f"{USERS}/athletes/{beto['id']}", headers=ana["headers"]).status_code == 404
    clock.advance(days=4)  # the temporal sanction ended: back to normal
    assert beto["id"] in ids_of(client.get(f"{USERS}/suggestions", headers=ana["headers"]))
    assert ids_of(cards()) == {beto["id"]}


def test_a_lifted_sanction_brings_the_account_back_and_a_permanent_one_keeps_it_out(clock):
    client, _, admin, ana, beto = world()
    first = sanction(client, admin, beto, tipo="permanente").json()
    assert beto["id"] not in ids_of(client.get(f"{USERS}/suggestions", headers=ana["headers"]))
    assert lift(client, admin, first["id"]).status_code == 200
    assert beto["id"] in ids_of(client.get(f"{USERS}/suggestions", headers=ana["headers"]))
