"""HU-47 (panel web): quién resolvió un reporte, cuándo y por qué; la cuenta que hizo la actividad o el mensaje
reportados; y sancionar desde esos reportes. Repositorio simulado y un servicio de autores falso; lo mismo contra
PostgreSQL está en test_reportes_autor_postgres.py."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from app.services.content_authors import AuthorResult, FOUND_NOTHING, UNAVAILABLE
from fakes import FakeEmailSender, register_and_verify

USERS = "/api/v1/users"
REPORTS = f"{USERS}/admin/reports"
SANCTIONS = f"{USERS}/admin/sanctions"
AUDIT = f"{USERS}/admin/audit-events"


class FakeAuthors:
    """What Ms_Activities and Ms_Matching would answer. `answers` maps (tipo, objetivo_id) to a result; `down` = no answer."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], AuthorResult] = {}
        self.down = False
        self.calls: list[tuple[str, str]] = []

    def lookup(self, tipo: str, objetivo_id: str) -> AuthorResult:
        self.calls.append((tipo, objetivo_id))
        if self.down:
            return UNAVAILABLE
        return self.answers.get((tipo, objetivo_id), FOUND_NOTHING)


def person(client, repository, nombre, role=None) -> dict:
    tokens = register_and_verify(client, {
        "email": f"{nombre.lower()}@example.com", "password": "A-secure-test-password-1",
        "nombre": nombre, "apellido_paterno": "Torres"})
    who = {"id": tokens["user"]["user_id"], "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}
    if role:
        uid = UUID(who["id"])
        repository._users[uid] = replace(repository._users[uid], role=role)
    return who


def setup():
    repository, authors = MockUserRepository(), FakeAuthors()
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender(), content_authors=authors))
    admin = person(client, repository, "Moderadora", "admin")
    ana, beto, carla = (person(client, repository, n) for n in ("Ana", "Beto", "Carla"))
    return client, repository, authors, admin, ana, beto, carla


def report(client, who, tipo, objetivo_id, motivo=None) -> dict:
    motivo = motivo or {"mensaje": "Lenguaje ofensivo"}.get(tipo, "Spam o publicidad")
    created = client.post(f"{USERS}/reports", headers=who["headers"], json={
        "tipo": tipo, "objetivo_id": objetivo_id, "objetivo_nombre": "Contenido reportado", "motivo": motivo,
        "detalle": "Detalle suficiente del reporte para la prueba"})
    assert created.status_code == 201, created.text
    return created.json()


def listing(client, admin, query="") -> list[dict]:
    response = client.get(f"{REPORTS}{query}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def audit(client, admin, query="") -> list[dict]:
    return client.get(f"{AUDIT}{query}", headers=admin["headers"]).json()["items"]


# -- quién lo resolvió, cuándo y por qué -------------------------------------------------------------------


def test_a_resolved_report_shows_who_resolved_it_when_and_the_note_and_the_audit_row_uses_the_same_data():
    client, _, _, admin, ana, beto, _ = setup()
    made = report(client, ana, "usuario", beto["id"])
    assert made["revisado_por"] is None and made["motivo_revision"] is None and made["fecha_revision"] is None
    done = client.patch(f"{REPORTS}/{made['id']}", headers=admin["headers"], json={"estado": "revisado", "motivo": "Se revisó y no hay falta"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["revisado_por"] == {"user_id": admin["id"], "nombre": "Moderadora", "apellido_inicial": "T."}
    assert body["motivo_revision"] == "Se revisó y no hay falta" and body["fecha_revision"] is not None
    [row] = [r for r in listing(client, admin, "?estado=todos") if r["id"] == made["id"]]
    assert (row["revisado_por"], row["motivo_revision"], row["fecha_revision"]) == (
        body["revisado_por"], body["motivo_revision"], body["fecha_revision"])
    [event] = audit(client, admin, "?accion=REPORT_REVIEWED")
    assert event["recurso_id"] == made["id"] and event["admin"]["id"] == admin["id"]
    assert event["motivo"] == body["motivo_revision"]                      # the same reason
    assert abs(datetime.fromisoformat(event["fecha"]) - datetime.fromisoformat(body["fecha_revision"])) < timedelta(seconds=5)


def test_without_a_note_the_audit_keeps_using_the_reason_of_the_report_and_the_note_stays_empty():
    client, _, _, admin, ana, beto, _ = setup()
    made = report(client, ana, "usuario", beto["id"], motivo="Perfil falso o suplantación")
    body = client.patch(f"{REPORTS}/{made['id']}", headers=admin["headers"], json={"estado": "descartado"}).json()
    assert body["motivo_revision"] is None and body["revisado_por"]["user_id"] == admin["id"]
    assert audit(client, admin, "?accion=REPORT_DISCARDED")[0]["motivo"] == "Perfil falso o suplantación"


def test_final_states_do_not_reopen_pending_is_the_default_and_the_limit_is_100():
    client, _, _, admin, ana, beto, carla = setup()
    first = report(client, ana, "usuario", beto["id"])
    second = report(client, ana, "usuario", carla["id"])
    client.patch(f"{REPORTS}/{first['id']}", headers=admin["headers"], json={"estado": "descartado"})
    assert client.patch(f"{REPORTS}/{first['id']}", headers=admin["headers"], json={"estado": "escalado"}).status_code == 409
    assert client.patch(f"{REPORTS}/{first['id']}", headers=admin["headers"], json={"estado": "revisado"}).status_code == 409
    assert [r["id"] for r in listing(client, admin)] == [second["id"]]                      # pending by default
    assert [r["id"] for r in listing(client, admin, "?estado=descartado")] == [first["id"]]
    assert len(listing(client, admin, "?estado=todos")) == 2
    assert client.get(f"{REPORTS}?limit=100", headers=admin["headers"]).status_code == 200
    assert client.get(f"{REPORTS}?limit=101", headers=admin["headers"]).status_code == 422
    revisado = report(client, beto, "usuario", carla["id"])
    client.patch(f"{REPORTS}/{revisado['id']}", headers=admin["headers"], json={"estado": "revisado"})
    assert client.patch(f"{REPORTS}/{revisado['id']}", headers=admin["headers"], json={"estado": "escalado"}).status_code == 200


# -- la cuenta que hizo la actividad o el mensaje -------------------------------------------------------------


def test_a_report_of_an_activity_or_a_message_carries_the_account_that_made_it():
    client, _, authors, admin, ana, beto, carla = setup()
    activity, message = str(uuid4()), f"{uuid4()}:42"
    authors.answers[("actividad", activity)] = AuthorResult("found", UUID(beto["id"]))
    authors.answers[("mensaje", message)] = AuthorResult("found", UUID(carla["id"]))
    r1, r2 = report(client, ana, "actividad", activity), report(client, ana, "mensaje", message)
    assert r1["reportado"] == {"user_id": beto["id"], "nombre": "Beto", "apellido_inicial": "T."} and r1["autor_no_disponible"] is False
    assert r2["reportado"]["user_id"] == carla["id"] and r2["autor_no_disponible"] is False
    rows = {r["id"]: r for r in listing(client, admin)}
    assert rows[r1["id"]]["reportado"]["user_id"] == beto["id"] and rows[r2["id"]]["reportado"]["user_id"] == carla["id"]
    assert set(rows[r1["id"]]["reportado"]) == {"user_id", "nombre", "apellido_inicial"}      # no e-mail, RUT or photo
    authors.calls.clear()
    listing(client, admin)
    assert authors.calls == []                                                                # remembered: not asked again
    # A report of a person still names that person and is never "without author".
    person_report = report(client, ana, "usuario", carla["id"])
    assert person_report["reportado"]["user_id"] == carla["id"] and person_report["autor_no_disponible"] is False


def test_if_the_origin_service_does_not_answer_the_report_goes_out_without_author_and_marked_then_it_is_found_later():
    client, _, authors, admin, ana, beto, _ = setup()
    activity = str(uuid4())
    authors.answers[("actividad", activity)] = AuthorResult("found", UUID(beto["id"]))
    authors.down = True
    made = report(client, ana, "actividad", activity)
    assert made["reportado"] is None and made["autor_no_disponible"] is True                  # nothing invented
    [row] = listing(client, admin)
    assert row["reportado"] is None and row["autor_no_disponible"] is True
    authors.down = False                                                                      # it comes back
    [row] = listing(client, admin)
    assert row["reportado"]["user_id"] == beto["id"] and row["autor_no_disponible"] is False


def test_a_missing_content_is_asked_once_and_stays_without_author():
    client, _, authors, admin, ana, _, _ = setup()
    gone = str(uuid4())
    made = report(client, ana, "actividad", gone)                                             # the service says "no such activity"
    assert made["reportado"] is None and made["autor_no_disponible"] is True
    authors.calls.clear()
    for _ in range(3):
        [row] = listing(client, admin)
        assert row["autor_no_disponible"] is True
    assert authors.calls == []                                                                # not asked again within the hour


def test_ids_that_are_not_valid_for_their_type_are_not_asked_at_all():
    client, _, authors, admin, ana, beto, carla = setup()
    authors.calls.clear()
    from app.services.content_authors import InternalContentAuthors
    real = InternalContentAuthors(None, None, "service-to-service-secret-with-32-bytes-min")
    assert real.lookup("actividad", "no-es-uuid").status == "missing"
    assert real.lookup("mensaje", "sin-dos-puntos").status == "missing"
    assert real.lookup("mensaje", f"{uuid4()}:abc").status == "missing"
    assert real.lookup("usuario", str(uuid4())).status == "missing"
    assert real.lookup("actividad", str(uuid4())).status == "unavailable"                    # valid id but no client configured


def test_without_any_author_source_configured_the_reports_still_work():
    repository = MockUserRepository()
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender()))
    admin = person(client, repository, "Moderadora", "admin")
    ana = person(client, repository, "Ana")
    made = report(client, ana, "mensaje", f"{uuid4()}:7")
    assert made["reportado"] is None and made["autor_no_disponible"] is True
    assert len(listing(client, admin)) == 1


def test_more_than_100_pending_reports_without_a_resolvable_author_do_not_slow_or_break_the_listing():
    """The local QA database has more than 100 pending reports about content that does not exist."""
    client, repository, authors, admin, ana, _, _ = setup()
    for n in range(130):
        report(client, ana, "actividad", str(uuid4()), motivo="Spam o publicidad")
        authors.calls.clear()
    # Created while the content "does not exist"; forget what was asked, as in a database loaded before this feature.
    for rid, rec in list(repository._reports.items()):
        repository._reports[rid] = replace(rec, autor_consultado_en=None)
    authors.calls.clear()
    page = listing(client, admin, "?limit=100")
    assert len(page) == 100 and all(r["autor_no_disponible"] for r in page)
    assert len(authors.calls) == 20                                                           # a few per request, not 100
    for _ in range(6):                                                                        # the rest are asked on the next requests
        listing(client, admin, "?limit=100")
    assert len(authors.calls) <= 130 and len(set(authors.calls)) == len(authors.calls)       # nobody is asked twice
    authors.down = True
    for rid, rec in list(repository._reports.items()):
        repository._reports[rid] = replace(rec, autor_consultado_en=None)
    authors.calls.clear()
    assert len(listing(client, admin, "?limit=100")) == 100
    assert len(authors.calls) == 1                                                            # service down: one try, the rest are not even asked
    assert len(listing(client, admin, "?estado=todos&limit=100&pagina=2")) == 30


# -- sancionar desde el reporte --------------------------------------------------------------------------


def sanction(client, admin, user_id, reporte_id, **extra):
    return client.post(SANCTIONS, headers=admin["headers"], json={
        "usuario_id": user_id, "tipo": "temporal", "dias": 7, "motivo": "Conducta reiterada", "reporte_id": reporte_id, **extra})


def test_an_escalated_report_of_an_activity_or_a_message_can_sanction_its_author_with_the_same_rules():
    client, _, authors, admin, ana, beto, carla = setup()
    activity, message = str(uuid4()), f"{uuid4()}:5"
    authors.answers[("actividad", activity)] = AuthorResult("found", UUID(beto["id"]))
    authors.answers[("mensaje", message)] = AuthorResult("found", UUID(carla["id"]))
    r1, r2 = report(client, ana, "actividad", activity), report(client, ana, "mensaje", message)
    # Same rules as HU-45: the report must be escalated first, then it must be about that account.
    assert sanction(client, admin, beto["id"], r1["id"]).status_code == 409
    for r in (r1, r2):
        assert client.patch(f"{REPORTS}/{r['id']}", headers=admin["headers"], json={"estado": "escalado"}).status_code == 200
    assert sanction(client, admin, ana["id"], r1["id"]).status_code == 422                    # not the author of that activity
    assert sanction(client, admin, carla["id"], r1["id"]).status_code == 422
    done = sanction(client, admin, beto["id"], r1["id"])
    assert done.status_code == 201, done.text
    assert done.json()["reporte_id"] == r1["id"] and done.json()["usuario"]["user_id"] == beto["id"]
    assert sanction(client, admin, carla["id"], r2["id"], tipo="permanente", dias=None).status_code == 201
    assert sanction(client, admin, beto["id"], r1["id"]).status_code == 409                   # already sanctioned
    assert {e["accion"] for e in audit(client, admin)} >= {"SANCTION_APPLIED", "REPORT_ESCALATED"}
    assert len(audit(client, admin, "?accion=SANCTION_APPLIED")) == 2
    # Their login is refused, like any sanction.
    login = client.post(f"{USERS}/auth/login", json={"email": "beto@example.com", "password": "A-secure-test-password-1"})
    assert login.status_code == 403 and login.json()["code"] == "account_suspended"


def test_a_report_without_a_known_author_cannot_sanction_anyone_and_an_admin_author_is_refused():
    client, repository, authors, admin, ana, beto, _ = setup()
    activity = str(uuid4())
    authors.down = True
    made = report(client, ana, "actividad", activity)
    client.patch(f"{REPORTS}/{made['id']}", headers=admin["headers"], json={"estado": "escalado"})
    assert sanction(client, admin, beto["id"], made["id"]).status_code == 422                 # unknown author: no guessing
    authors.down = False
    other_admin = person(client, repository, "Otraadmin", "admin")
    authors.answers[("actividad", activity)] = AuthorResult("found", UUID(other_admin["id"]))
    listing(client, admin)                                                                    # the author is found when listing
    assert sanction(client, admin, other_admin["id"], made["id"]).status_code == 422          # an admin cannot be sanctioned
    assert sanction(client, admin, admin["id"], made["id"]).status_code == 422


def test_only_an_administrator_sees_or_resolves_reports_and_the_author_never_leaks_to_the_reporter():
    client, _, authors, admin, ana, beto, _ = setup()
    activity = str(uuid4())
    authors.answers[("actividad", activity)] = AuthorResult("found", UUID(beto["id"]))
    made = report(client, ana, "actividad", activity)
    assert client.get(REPORTS, headers=ana["headers"]).status_code == 403
    assert client.patch(f"{REPORTS}/{made['id']}", headers=ana["headers"], json={"estado": "revisado"}).status_code == 403
    assert client.get(REPORTS).status_code == 401


# -- el cliente real, contra un servidor de prueba local -------------------------------------------------------

import json as _json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.content_authors import InternalContentAuthors

TOKEN = "service-to-service-secret-with-32-bytes-min"
KNOWN_ACTIVITY, KNOWN_MATCH = str(uuid4()), str(uuid4())
AUTHOR = uuid4()


class _Origin(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_GET(self):  # noqa: N802
        _Origin.seen.append((self.path, self.headers.get("X-Internal-Service-Token")))
        if self.headers.get("X-Internal-Service-Token") != TOKEN:
            return self._send(401, {"detail": "service authentication required"})
        if self.path == f"/internal/v1/activities/{KNOWN_ACTIVITY}/author":
            return self._send(200, {"user_id": str(AUTHOR)})
        if self.path == f"/internal/v1/messages/{KNOWN_MATCH}/42/author":
            return self._send(200, {"user_id": str(AUTHOR)})
        if self.path.endswith("/broken/author"):
            return self._send(200, {"otra": "cosa"})
        if "/slow/" in self.path:
            time.sleep(1.5)
        if "/boom/" in self.path:
            return self._send(500, {"detail": "error interno"})
        self._send(404, {"detail": "not found"})

    def _send(self, code, body):
        data = _json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silent
        pass


@pytest.fixture
def origin():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Origin.seen.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_the_real_client_asks_each_service_with_the_secret_and_tells_the_four_outcomes_apart(origin):
    client = InternalContentAuthors(origin, origin, TOKEN, timeout=0.5)
    assert client.lookup("actividad", KNOWN_ACTIVITY) == AuthorResult("found", AUTHOR)
    assert client.lookup("mensaje", f"{KNOWN_MATCH}:42") == AuthorResult("found", AUTHOR)
    assert client.lookup("actividad", str(uuid4())).status == "missing"                       # 404: the content is gone
    assert client.lookup("mensaje", f"{KNOWN_MATCH}:43").status == "missing"
    assert all(token == TOKEN for _, token in _Origin.seen)                                   # always with the secret
    assert client.lookup("actividad", "boom").status == "missing"                             # not even a valid id: not asked
    assert InternalContentAuthors(origin, origin, "otro-secreto-que-no-es-el-bueno-1234567", 0.5).lookup(
        "actividad", KNOWN_ACTIVITY).status == "unavailable"                                  # 401: refused, not "no author"
    assert InternalContentAuthors(origin, origin, None, 0.5).lookup("actividad", KNOWN_ACTIVITY).status == "unavailable"


def test_the_real_client_never_invents_an_author_when_the_origin_fails(origin):
    client = InternalContentAuthors(origin, origin, TOKEN, timeout=0.5)
    boom = InternalContentAuthors(origin + "/boom", origin, TOKEN, timeout=0.5)
    assert boom.lookup("actividad", str(uuid4())).status == "unavailable"                     # 500
    slow = InternalContentAuthors(origin + "/slow", origin, TOKEN, timeout=0.3)
    started = time.monotonic()
    assert slow.lookup("actividad", str(uuid4())).status == "unavailable"                     # timeout
    assert time.monotonic() - started < 1.2
    broken = InternalContentAuthors(origin + "/broken", origin, TOKEN, timeout=0.5)
    assert broken.lookup("actividad", str(UUID(int=1))).status in ("unavailable", "missing")
    dead = InternalContentAuthors("http://127.0.0.1:9", "http://127.0.0.1:9", TOKEN, timeout=0.5)
    assert dead.lookup("actividad", str(uuid4())).status == "unavailable"                     # nobody listening
    assert dead.lookup("mensaje", f"{uuid4()}:1").status == "unavailable"
    assert client.lookup("usuario", str(uuid4())).status == "missing"
