"""A number the JSON cannot carry (1e999, Infinity, NaN) must be a 422, never a server error."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_suggestions import make_client, signed_up


def put_zone(client: TestClient, who: dict, latitud: str):
    body = ('{"deportes": [], "disponibilidad": [], "objetivos": [], '
            '"zona": {"comuna": "Ñuñoa", "latitud": %s, "longitud": 0}}' % latitud)
    return client.put(f"/api/v1/users/{who['user_id']}/preferences",
                      headers={**who["headers"], "Content-Type": "application/json"}, content=body)


def test_valid_and_finite_invalid_numbers_behave_as_before():
    client, _ = make_client()
    who = signed_up(client, "ana@example.com")
    assert put_zone(client, who, "-33.4").status_code == 200
    bad = put_zone(client, who, "999")
    assert bad.status_code == 422
    error = bad.json()["detail"][0]
    assert error["loc"][-1] == "latitud" and error["input"] == 999 and error["type"] == "less_than_equal"


@pytest.mark.parametrize("literal", ["1e999", "-1e999", "Infinity", "-Infinity", "NaN"])
def test_a_non_finite_number_is_a_422_that_names_the_field(literal):
    client, _ = make_client()
    who = signed_up(client, "ana@example.com")
    quiet = TestClient(client.app, raise_server_exceptions=False)  # a crash must show up as 500, not as an exception
    response = put_zone(quiet, who, literal)
    assert response.status_code == 422, response.text
    error = response.json()["detail"][0]
    assert error["loc"][-1] == "latitud" and isinstance(error["input"], str)  # echoed as text: "inf", "-inf", "nan"
    assert {"type", "loc", "msg", "input"} <= set(error)  # same shape as FastAPI's own 422
