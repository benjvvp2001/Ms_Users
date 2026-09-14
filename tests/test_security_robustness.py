"""Pruebas de robustez/seguridad con datos correctos e incorrectos.

Objetivo: confirmar que el servicio NO responde "si" (2xx) ante datos
invalidos, malformados o maliciosos, y que los controles de autenticacion/
autorizacion (JWT, ownership) se aplican correctamente. Usa el repositorio
mock (sin base de datos real), igual que tests/test_users_api.py.
"""

from __future__ import annotations

import os
from uuid import uuid4

os.environ.setdefault("JWT_SECRET", "test-only-secret-that-is-long-enough")

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository

VALID_PASSWORD = "a-secure-test-password"


def make_client() -> TestClient:
    return TestClient(create_app(repository=MockUserRepository()))


def register_payload(**overrides: object) -> dict:
    payload = {
        "email": "ana@example.com",
        "password": VALID_PASSWORD,
        "nombre": "Ana",
        "apellido_paterno": "Torres",
    }
    payload.update(overrides)
    return payload


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Registro: dato correcto de control
# ---------------------------------------------------------------------------

def test_registro_con_datos_correctos_es_aceptado() -> None:
    client = make_client()
    response = client.post("/api/v1/users/auth/register", json=register_payload())
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# 2. Registro: datos incorrectos / incompletos deben ser RECHAZADOS (422/409)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "descripcion,overrides,campo_eliminado",
    [
        ("email sin arroba", {"email": "ana-example.com"}, None),
        ("email vacio", {"email": ""}, None),
        ("password muy corta (11 chars, minimo 12)", {"password": "corta12345a"}, None),
        ("password vacia", {"password": ""}, None),
        ("nombre vacio", {"nombre": ""}, None),
        ("nombre excede 50 caracteres", {"nombre": "a" * 51}, None),
        ("rut excede 20 caracteres", {"rut": "1" * 21}, None),
    ],
)
def test_registro_con_datos_incorrectos_es_rechazado(
    descripcion: str, overrides: dict, campo_eliminado: str | None
) -> None:
    client = make_client()
    payload = register_payload(**overrides)
    response = client.post("/api/v1/users/auth/register", json=payload)
    assert response.status_code == 422, f"Caso '{descripcion}' deberia dar 422, dio {response.status_code}: {response.text}"


@pytest.mark.parametrize("campo_faltante", ["email", "password", "nombre", "apellido_paterno"])
def test_registro_con_campo_obligatorio_faltante_es_rechazado(campo_faltante: str) -> None:
    client = make_client()
    payload = register_payload()
    del payload[campo_faltante]
    response = client.post("/api/v1/users/auth/register", json=payload)
    assert response.status_code == 422


def test_registro_con_campo_extra_no_declarado_es_rechazado() -> None:
    """extra='forbid' en el schema: no debe aceptar campos no contemplados
    (p.ej. intentar inyectar role=admin desde el registro)."""
    client = make_client()
    payload = register_payload(role="admin")
    response = client.post("/api/v1/users/auth/register", json=payload)
    assert response.status_code == 422


def test_registro_con_tipo_de_dato_incorrecto_es_rechazado() -> None:
    client = make_client()
    payload = register_payload(password=12345678901234)  # numero en vez de string
    response = client.post("/api/v1/users/auth/register", json=payload)
    assert response.status_code == 422


def test_registro_con_email_duplicado_es_rechazado() -> None:
    client = make_client()
    assert client.post("/api/v1/users/auth/register", json=register_payload()).status_code == 201
    dup = client.post("/api/v1/users/auth/register", json=register_payload())
    assert dup.status_code == 409


def test_registro_con_email_duplicado_case_insensitive_es_rechazado() -> None:
    """ANA@example.com y ana@example.com deben tratarse como el mismo email."""
    client = make_client()
    assert client.post("/api/v1/users/auth/register", json=register_payload()).status_code == 201
    dup = client.post(
        "/api/v1/users/auth/register", json=register_payload(email="ANA@EXAMPLE.COM")
    )
    assert dup.status_code == 409


def test_registro_con_json_malformado_es_rechazado() -> None:
    client = make_client()
    response = client.post(
        "/api/v1/users/auth/register",
        content="{ esto no es json valido ",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 3. Registro: payloads maliciosos deben aceptarse SOLO como texto literal
#    (no deben romper el servicio ni ejecutarse) o ser rechazados por longitud
# ---------------------------------------------------------------------------

def test_registro_con_sql_injection_en_nombre_no_compromete_el_servicio() -> None:
    client = make_client()
    payload = register_payload(
        email="sqltest@example.com",
        nombre="Robert'); DROP TABLE users;--",
    )
    response = client.post("/api/v1/users/auth/register", json=payload)
    # Debe aceptarse como texto literal (Pydantic/ORM parametrizado) y
    # devolverse tal cual, sin causar error 500 ni alterar otros registros.
    assert response.status_code == 201
    assert response.json()["user"]["nombre"] == "Robert'); DROP TABLE users;--"


def test_registro_con_xss_en_nombre_se_almacena_como_texto_literal() -> None:
    client = make_client()
    payload = register_payload(
        email="xsstest@example.com",
        nombre="<script>alert(1)</script>",
    )
    response = client.post("/api/v1/users/auth/register", json=payload)
    assert response.status_code == 201
    # La API debe devolver el string literal (sin ejecutar/alterar el
    # payload); el escapado para HTML es responsabilidad del frontend.
    assert response.json()["user"]["nombre"] == "<script>alert(1)</script>"


# ---------------------------------------------------------------------------
# 4. Login: correcto vs incorrecto
# ---------------------------------------------------------------------------

def test_login_con_credenciales_correctas_es_aceptado() -> None:
    client = make_client()
    client.post("/api/v1/users/auth/register", json=register_payload())
    response = client.post(
        "/api/v1/users/auth/login",
        json={"email": "ana@example.com", "password": VALID_PASSWORD},
    )
    assert response.status_code == 200


def test_login_con_password_incorrecta_es_rechazado() -> None:
    client = make_client()
    client.post("/api/v1/users/auth/register", json=register_payload())
    response = client.post(
        "/api/v1/users/auth/login",
        json={"email": "ana@example.com", "password": "password-incorrecta"},
    )
    assert response.status_code == 401


def test_login_con_email_inexistente_es_rechazado() -> None:
    client = make_client()
    response = client.post(
        "/api/v1/users/auth/login",
        json={"email": "no-existe@example.com", "password": VALID_PASSWORD},
    )
    assert response.status_code == 401


def test_login_no_filtra_si_el_email_existe_o_no() -> None:
    """El mensaje de error debe ser identico para email inexistente y para
    password incorrecta, para no revelar que emails estan registrados
    (user enumeration)."""
    client = make_client()
    client.post("/api/v1/users/auth/register", json=register_payload())

    wrong_password = client.post(
        "/api/v1/users/auth/login",
        json={"email": "ana@example.com", "password": "password-incorrecta"},
    )
    unknown_email = client.post(
        "/api/v1/users/auth/login",
        json={"email": "no-existe@example.com", "password": VALID_PASSWORD},
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


def test_login_con_campos_faltantes_es_rechazado() -> None:
    client = make_client()
    response = client.post("/api/v1/users/auth/login", json={"email": "ana@example.com"})
    assert response.status_code == 422


def test_login_con_email_invalido_es_rechazado() -> None:
    client = make_client()
    response = client.post(
        "/api/v1/users/auth/login",
        json={"email": "no-es-un-email", "password": VALID_PASSWORD},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 5. Autenticacion / autorizacion: acceso sin token, con token invalido,
#    o a recursos de otro usuario (IDOR)
# ---------------------------------------------------------------------------

def test_acceso_sin_token_es_rechazado() -> None:
    client = make_client()
    response = client.get(f"/api/v1/users/{uuid4()}/profile")
    assert response.status_code == 401


def test_acceso_con_token_invalido_es_rechazado() -> None:
    client = make_client()
    response = client.get(
        f"/api/v1/users/{uuid4()}/profile",
        headers=auth_headers("esto-no-es-un-jwt-valido"),
    )
    assert response.status_code == 401


def test_acceso_con_token_manipulado_es_rechazado() -> None:
    """Token firmado con OTRO secreto (simula un token forjado por un
    atacante) debe ser rechazado, no aceptado por coincidencia de forma."""
    client = make_client()
    forged = jwt.encode({"sub": str(uuid4())}, "secreto-incorrecto", algorithm="HS256")
    response = client.get(
        f"/api/v1/users/{uuid4()}/profile",
        headers=auth_headers(forged),
    )
    assert response.status_code == 401


def test_acceso_con_token_alg_none_es_rechazado() -> None:
    """Ataque clasico alg=none: un token sin firma no debe ser aceptado."""
    client = make_client()
    forged = jwt.encode({"sub": str(uuid4())}, "", algorithm="none")
    response = client.get(
        f"/api/v1/users/{uuid4()}/profile",
        headers=auth_headers(forged),
    )
    assert response.status_code == 401


def test_acceso_a_perfil_de_otro_usuario_es_rechazado_idor() -> None:
    client = make_client()
    victim = client.post(
        "/api/v1/users/auth/register", json=register_payload(email="victima@example.com")
    ).json()
    attacker = client.post(
        "/api/v1/users/auth/register", json=register_payload(email="atacante@example.com")
    ).json()

    response = client.get(
        f"/api/v1/users/{victim['user']['user_id']}/profile",
        headers=auth_headers(attacker["access_token"]),
    )
    assert response.status_code == 403


def test_id_de_usuario_malformado_en_la_url_es_rechazado() -> None:
    client = make_client()
    response = client.get(
        "/api/v1/users/no-es-un-uuid/profile",
        headers=auth_headers("cualquier-token"),
    )
    assert response.status_code in (401, 422)


# ---------------------------------------------------------------------------
# 6. Reglas de negocio en preferencias / disponibilidad
# ---------------------------------------------------------------------------

def _registrar_y_loguear(client: TestClient) -> tuple[str, dict]:
    registration = client.post(
        "/api/v1/users/auth/register", json=register_payload()
    ).json()
    return registration["user"]["user_id"], auth_headers(registration["access_token"])


@pytest.mark.parametrize(
    "descripcion,preferencias",
    [
        ("nivel de deporte fuera de rango (6 > 5)", {
            "deportes": [{"deporte_codigo": "tenis", "nivel": 6}],
        }),
        ("nivel de deporte fuera de rango (0 < 1)", {
            "deportes": [{"deporte_codigo": "tenis", "nivel": 0}],
        }),
        ("deportes duplicados", {
            "deportes": [
                {"deporte_codigo": "tenis", "nivel": 3},
                {"deporte_codigo": "TENIS", "nivel": 4},
            ],
        }),
        ("hora_fin anterior a hora_inicio", {
            "disponibilidad": [
                {"dia_semana": "lunes", "hora_inicio": "20:00", "hora_fin": "18:00"}
            ],
        }),
        ("hora con formato invalido", {
            "disponibilidad": [
                {"dia_semana": "lunes", "hora_inicio": "25:00", "hora_fin": "26:00"}
            ],
        }),
        ("dia_semana invalido", {
            "disponibilidad": [
                {"dia_semana": "funday", "hora_inicio": "18:00", "hora_fin": "20:00"}
            ],
        }),
    ],
)
def test_preferencias_con_datos_invalidos_son_rechazadas(
    descripcion: str, preferencias: dict
) -> None:
    client = make_client()
    user_id, headers = _registrar_y_loguear(client)
    response = client.put(
        f"/api/v1/users/{user_id}/preferences", headers=headers, json=preferencias
    )
    assert response.status_code == 422, f"Caso '{descripcion}' deberia dar 422, dio {response.status_code}: {response.text}"


def test_preferencias_con_datos_correctos_son_aceptadas() -> None:
    client = make_client()
    user_id, headers = _registrar_y_loguear(client)
    response = client.put(
        f"/api/v1/users/{user_id}/preferences",
        headers=headers,
        json={
            "deportes": [{"deporte_codigo": "tenis", "nivel": 3}],
            "disponibilidad": [
                {"dia_semana": "lunes", "hora_inicio": "18:00", "hora_fin": "20:00"}
            ],
        },
    )
    assert response.status_code == 200
