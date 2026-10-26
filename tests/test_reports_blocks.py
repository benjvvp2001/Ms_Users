from __future__ import annotations

import os
from dataclasses import replace
from uuid import UUID, uuid4

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from fakes import FakeEmailSender, register_and_verify

USERS = "/api/v1/users"


def make_client() -> tuple[TestClient, MockUserRepository]:
    repository = MockUserRepository()
    return TestClient(create_app(repository=repository, email_sender=FakeEmailSender())), repository


def athlete(client: TestClient, nombre: str) -> dict:
    tokens = register_and_verify(client, {
        "email": f"{nombre.lower()}@example.com", "password": "A-secure-test-password-1",
        "nombre": nombre, "apellido_paterno": "Torres",
    })
    return {"id": tokens["user"]["user_id"], "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}


def suggested(client: TestClient, who: dict) -> set[str]:
    response = client.get(f"{USERS}/suggestions", headers=who["headers"])
    assert response.status_code == 200, response.text
    return {card["user_id"] for card in response.json()}


def test_a_block_hides_both_people_from_each_other_in_discover() -> None:
    client, _ = make_client()
    ana, beto, carla = athlete(client, "Ana"), athlete(client, "Beto"), athlete(client, "Carla")
    assert suggested(client, ana) == {beto["id"], carla["id"]}

    assert client.put(f"{USERS}/me/blocks/{beto['id']}", headers=ana["headers"]).status_code == 204
    assert client.put(f"{USERS}/me/blocks/{beto['id']}", headers=ana["headers"]).status_code == 204  # Idempotent.
    assert suggested(client, ana) == {carla["id"]}
    assert suggested(client, beto) == {carla["id"]}          # Beto stops seeing Ana too.
    assert suggested(client, carla) == {ana["id"], beto["id"]}

    blocked = client.get(f"{USERS}/me/blocks", headers=ana["headers"]).json()
    assert [(b["user_id"], b["nombre"], b["apellido_inicial"]) for b in blocked] == [(beto["id"], "Beto", "T.")]
    assert client.get(f"{USERS}/me/blocks", headers=beto["headers"]).json() == []
    # Since HU-51 a block also withholds the athlete card both ways (that is what stops requests and
    # messages in Matching); the full behaviour is covered in test_blocks_mutes.py.
    assert client.get(f"{USERS}/athletes/{beto['id']}", headers=ana["headers"]).status_code == 404

    assert client.delete(f"{USERS}/me/blocks/{beto['id']}", headers=ana["headers"]).status_code == 204
    assert client.get(f"{USERS}/athletes/{beto['id']}", headers=ana["headers"]).status_code == 200
    assert suggested(client, ana) == {beto["id"], carla["id"]}
    assert suggested(client, beto) == {ana["id"], carla["id"]}


def test_who_cannot_be_blocked() -> None:
    client, _ = make_client()
    ana = athlete(client, "Ana")
    assert client.put(f"{USERS}/me/blocks/{ana['id']}", headers=ana["headers"]).status_code == 422
    assert client.put(f"{USERS}/me/blocks/{uuid4()}", headers=ana["headers"]).status_code == 404
    assert client.put(f"{USERS}/me/blocks/{uuid4()}").status_code == 401


JUSTIFICATION = "Me escribió de madrugada"


def club(client: TestClient, nombre: str) -> dict:
    tokens = register_and_verify(client, {
        "email": f"{nombre.lower()}@club.example.com", "password": "A-secure-test-password-1",
        "nombre": nombre, "apellido_paterno": "Soto", "tipo": "club",
    })
    assert tokens["user"]["role"] == "club_admin"
    return {"id": tokens["user"]["user_id"], "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}


def report(client: TestClient, who: dict, **changes: str):
    body = {"tipo": "usuario", "objetivo_id": str(uuid4()), "objetivo_nombre": "Beto T.",
            "motivo": "Spam o publicidad", "detalle": JUSTIFICATION, **changes}
    return client.post(f"{USERS}/reports", headers=who["headers"], json=body)


def test_reports_are_validated_and_counted_once_per_reason() -> None:
    client, _ = make_client()
    ana, beto = athlete(client, "Ana"), athlete(client, "Beto")
    created = report(client, ana, objetivo_id=beto["id"])
    assert created.status_code == 201, created.text
    assert created.json()["estado"] == "pendiente"
    assert created.json()["reportante"]["user_id"] == ana["id"]
    assert report(client, ana, objetivo_id=beto["id"]).status_code == 409
    assert report(client, ana, objetivo_id=beto["id"], motivo="Lenguaje ofensivo").status_code == 201
    assert report(client, beto, objetivo_id=beto["id"]).status_code == 422          # Yourself.
    assert report(client, ana, motivo="Lugar inseguro").status_code == 422          # Not a user reason.
    assert report(client, ana, motivo="Otro", detalle="corto").status_code == 422
    assert report(client, ana, motivo="Otro", detalle="Me escribió de madrugada").status_code == 201
    assert report(client, ana, tipo="actividad", motivo="Lugar inseguro",
                  objetivo_nombre="Trote nocturno").status_code == 201
    assert report(client, ana, tipo="mensaje", objetivo_id="match-1:42", motivo="Spam o estafa").status_code == 201
    assert client.post(f"{USERS}/reports", json={}).status_code == 401


def test_every_report_needs_a_written_justification_between_10_and_300_characters() -> None:
    client, _ = make_client()
    ana, beto = athlete(client, "Ana"), athlete(client, "Beto")
    target = beto["id"]
    for motivo in ("Spam o publicidad", "Otro"):              # Not only "Otro".
        assert report(client, ana, objetivo_id=target, motivo=motivo, detalle="").status_code == 422
        assert report(client, ana, objetivo_id=target, motivo=motivo, detalle="   ").status_code == 422
        assert report(client, ana, objetivo_id=target, motivo=motivo, detalle="nueve car").status_code == 422
    body = {"tipo": "usuario", "objetivo_id": target, "objetivo_nombre": "Beto T.", "motivo": "Spam o publicidad"}
    assert client.post(f"{USERS}/reports", headers=ana["headers"], json=body).status_code == 422  # Missing.
    assert report(client, ana, objetivo_id=target, detalle="Diez letra").status_code == 201        # Exactly 10.
    assert report(client, ana, objetivo_id=target, motivo="Lenguaje ofensivo",
                  detalle="x" * 300).status_code == 201                                            # Exactly 300.
    assert report(client, ana, objetivo_id=target, motivo="Acoso o trato irrespetuoso",
                  detalle="x" * 301).status_code == 422


def test_clubs_can_report_people_and_the_report_keeps_the_club_role() -> None:
    client, repository = make_client()
    rocio, beto = club(client, "Rocio"), athlete(client, "Beto")
    admin = athlete(client, "Moderadora")
    repository._users[UUID(admin["id"])] = replace(repository._users[UUID(admin["id"])], role="admin")

    created = report(client, rocio, objetivo_id=beto["id"], motivo="Perfil falso o suplantación",
                     detalle="Usa fotos de otra persona")
    assert created.status_code == 201, created.text
    assert created.json()["reportante"]["user_id"] == rocio["id"]
    assert created.json()["reportante_rol"] == "club_admin"

    listed = client.get(f"{USERS}/admin/reports", headers=admin["headers"]).json()
    assert [(r["reportante_rol"], r["reportante"]["user_id"]) for r in listed] == [("club_admin", rocio["id"])]
    # The reported person has no route to see who reported, and no reporter data in their own profile.
    assert client.get(f"{USERS}/admin/reports", headers=beto["headers"]).status_code == 403
    own_profile = client.get(f"{USERS}/{beto['id']}/profile", headers=beto["headers"])
    assert own_profile.status_code == 200 and rocio["id"] not in own_profile.text
    assert "reportante" not in own_profile.text


def test_the_reporter_role_is_the_one_held_when_reporting() -> None:
    client, repository = make_client()
    ana, beto = athlete(client, "Ana"), athlete(client, "Beto")
    created = report(client, ana, objetivo_id=beto["id"]).json()
    assert created["reportante_rol"] == "player"
    # A later role change does not rewrite the report.
    repository._users[UUID(ana["id"])] = replace(repository._users[UUID(ana["id"])], role="club_admin")
    admin = athlete(client, "Moderadora")
    repository._users[UUID(admin["id"])] = replace(repository._users[UUID(admin["id"])], role="admin")
    listed = client.get(f"{USERS}/admin/reports", headers=admin["headers"]).json()
    assert [r["reportante_rol"] for r in listed] == ["player"]


def test_only_admins_list_and_review_reports() -> None:
    client, repository = make_client()
    ana, admin = athlete(client, "Ana"), athlete(client, "Moderadora")
    admin_id = UUID(admin["id"])
    repository._users[admin_id] = replace(repository._users[admin_id], role="admin")
    first = report(client, ana).json()
    report(client, ana, motivo="Lenguaje ofensivo")

    assert client.get(f"{USERS}/admin/reports", headers=ana["headers"]).status_code == 403
    pending = client.get(f"{USERS}/admin/reports?estado=pendiente", headers=admin["headers"]).json()
    assert [r["motivo"] for r in pending] == ["Lenguaje ofensivo", "Spam o publicidad"]   # Newest first.

    patch = f"{USERS}/admin/reports/{first['id']}"
    assert client.patch(patch, headers=ana["headers"], json={"estado": "revisado"}).status_code == 403
    reviewed = client.patch(patch, headers=admin["headers"], json={"estado": "revisado"})
    assert reviewed.status_code == 200 and reviewed.json()["fecha_revision"] is not None
    assert client.patch(patch, headers=admin["headers"], json={"estado": "pendiente"}).status_code == 422
    assert client.patch(f"{USERS}/admin/reports/{uuid4()}", headers=admin["headers"],
                        json={"estado": "descartado"}).status_code == 404
    assert [r["id"] for r in client.get(f"{USERS}/admin/reports?estado=revisado",
                                        headers=admin["headers"]).json()] == [first["id"]]
    assert len(client.get(f"{USERS}/admin/reports", headers=admin["headers"]).json()) == 2
