"""Live position: the app reports it after moving; it ranks nearby people and is never returned."""
from __future__ import annotations

import os
from datetime import timedelta

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest

from app.core.recommendations import distance_km
from app.repositories.base import LocationRecord
from app.schemas.user import Zona
from test_recommendations import cards, preferences
from test_suggestions import make_client, signed_up

LOCATION = "/api/v1/users/location"
# 0.001 degrees of latitude are about 111 m; the default minimum move is 200 m.
SMALL, BIG = 0.001, 0.003


def put(client, user, lat, lon=0.0, **extra):
    return client.put(LOCATION, headers=user["headers"], json={"latitud": lat, "longitud": lon, **extra})


def stored(repository, user):
    return repository.get_location(__import__("uuid").UUID(user["user_id"]))


def test_requires_a_token_and_never_returns_coordinates():
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    for call in (client.get, client.delete):
        assert call(LOCATION).status_code == 401
    assert client.put(LOCATION, json={"latitud": 1, "longitud": 1}).status_code == 401
    body = put(client, me, -33.45, -70.66, precision_m=12).json()
    assert body["has_location"] and body["updated"] is True
    assert body["min_distance_meters"] == 200 and body["max_age_minutes"] == 1440
    assert "-33.45" not in str(body) and "latitud" not in body
    assert "-33.45" not in client.get(LOCATION, headers=me["headers"]).text


@pytest.mark.parametrize("payload", [
    {"latitud": 91, "longitud": 0}, {"latitud": 0, "longitud": 181}, {"latitud": 0}, {"longitud": 0},
    {"latitud": "x", "longitud": 0}, {"latitud": 0, "longitud": 0, "precision_m": -1},
    {"latitud": 0, "longitud": 0, "extra": 1},
])
def test_invalid_positions_are_refused(payload):
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    assert client.put(LOCATION, headers=me["headers"], json=payload).status_code == 422
    assert stored(repository, me) is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_numbers_are_refused_not_a_server_error(literal):
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    response = client.put(LOCATION, headers={**me["headers"], "Content-Type": "application/json"},
                          content='{"latitud": %s, "longitud": 0}' % literal)
    assert response.status_code == 422
    assert stored(repository, me) is None


def test_a_small_move_keeps_the_position_and_a_big_one_replaces_it():
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    assert put(client, me, 0.0).json()["updated"] is True
    first = stored(repository, me)
    assert put(client, me, SMALL).json() == {**client.get(LOCATION, headers=me["headers"]).json(), "updated": False}
    assert stored(repository, me).latitud == first.latitud  # position unchanged
    assert put(client, me, BIG).json()["updated"] is True
    assert stored(repository, me).latitud == BIG


def test_a_still_person_is_renewed_so_the_position_does_not_expire():
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    put(client, me, 0.0)
    user_id = __import__("uuid").UUID(me["user_id"])
    old = repository._locations[user_id]
    from dataclasses import replace
    repository._locations[user_id] = replace(old, actualizada_en=old.actualizada_en - timedelta(minutes=30))
    aged = repository._locations[user_id].actualizada_en
    assert put(client, me, SMALL).json()["updated"] is False
    renewed = repository._locations[user_id]
    assert renewed.actualizada_en > aged and renewed.latitud == old.latitud
    before = renewed.actualizada_en
    put(client, me, SMALL)  # just renewed: nothing is written again
    assert repository._locations[user_id].actualizada_en == before


def test_delete_removes_the_position_and_is_repeatable():
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    put(client, me, 0.0)
    assert client.delete(LOCATION, headers=me["headers"]).status_code == 204
    assert client.delete(LOCATION, headers=me["headers"]).status_code == 204
    assert client.get(LOCATION, headers=me["headers"]).json()["has_location"] is False
    assert stored(repository, me) is None


def test_deleting_the_account_removes_the_position():
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    put(client, me, 0.0)
    assert client.delete(f"/api/v1/users/{me['user_id']}", headers=me["headers"]).status_code in (200, 204)
    assert stored(repository, me) is None


def test_only_athletes_share_a_position():
    client, repository = make_client()
    owner = signed_up(client, "owner@example.com")
    user_id = __import__("uuid").UUID(owner["user_id"])
    repository._users[user_id].role = "club_admin" if hasattr(repository._users[user_id], "role") else "club_admin"
    for response in (put(client, owner, 0.0), client.get(LOCATION, headers=owner["headers"]),
                     client.delete(LOCATION, headers=owner["headers"])):
        assert response.status_code == 403
    assert stored(repository, owner) is None


def test_the_live_position_decides_who_is_near_and_beats_the_profile_zone():
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    preferences(client, me, lat=0, lon=0, sports=[("running", 3)])
    home = signed_up(client, "home@example.com")       # profile zone is 2 km from my zone...
    preferences(client, home, lat=0.018, lon=0, sports=[("running", 3)])
    away = signed_up(client, "away@example.com")        # ...and this one is 12 km away
    preferences(client, away, lat=0.108, lon=0, sports=[("running", 3)])
    assert [c["user_id"] for c in cards(client, me, radius_km=5)] == [home["user_id"]]
    # I travel next to "away"; "away" also reports being there, "home" stays on its fixed zone.
    put(client, me, 0.108)
    put(client, away, 0.109)
    near = cards(client, me, radius_km=5)
    assert [c["user_id"] for c in near] == [away["user_id"]]
    assert near[0]["distancia_km"] == pytest.approx(0.1, abs=0.1)
    assert "latitud" not in str(near) and "longitud" not in str(near)


def test_a_candidate_who_moved_is_found_by_the_new_place_not_the_old_zone():
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    preferences(client, me, lat=0, lon=0, sports=[("running", 3)])
    runner = signed_up(client, "runner@example.com")
    preferences(client, runner, lat=0.018, lon=0, sports=[("running", 3)])     # zone near me
    assert [c["user_id"] for c in cards(client, me, radius_km=5)] == [runner["user_id"]]
    put(client, runner, 0.5)                                                    # now ~55 km away
    assert cards(client, me, radius_km=5) == []


def test_an_old_position_is_ignored_and_the_profile_zone_is_used_again():
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    preferences(client, me, lat=0, lon=0, sports=[("running", 3)])
    runner = signed_up(client, "runner@example.com")
    preferences(client, runner, lat=0.018, lon=0, sports=[("running", 3)])
    put(client, runner, 0.5)
    assert cards(client, me, radius_km=5) == []
    from dataclasses import replace
    runner_id = __import__("uuid").UUID(runner["user_id"])
    stale = repository._locations[runner_id]
    repository._locations[runner_id] = replace(stale, actualizada_en=stale.actualizada_en - timedelta(days=2))
    assert [c["user_id"] for c in cards(client, me, radius_km=5)] == [runner["user_id"]]


def test_the_radius_works_from_my_live_position_even_without_a_profile_zone():
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    preferences(client, me, sports=[("running", 3)])               # no zone at all
    other = signed_up(client, "other@example.com")
    preferences(client, other, lat=0.018, lon=0, sports=[("running", 3)])
    assert client.get("/api/v1/users/suggestions", headers=me["headers"], params={"radius_km": 5}).status_code == 422
    put(client, me, 0.0)
    assert [c["user_id"] for c in cards(client, me, radius_km=5)] == [other["user_id"]]


def test_the_thresholds_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LOCATION_MIN_MOVE_METERS", "50")
    monkeypatch.setenv("LOCATION_MAX_AGE_MINUTES", "60")
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    body = put(client, me, 0.0).json()
    assert (body["min_distance_meters"], body["max_age_minutes"]) == (50, 60)
    assert put(client, me, 0.0003).json()["updated"] is False       # ~33 m: below 50
    assert put(client, me, 0.001).json()["updated"] is True         # ~111 m: above 50


def test_my_own_old_position_is_not_used_as_the_origin():
    client, repository = make_client()
    me = signed_up(client, "me@example.com")
    preferences(client, me, lat=0, lon=0, sports=[("running", 3)])
    home = signed_up(client, "home@example.com")
    preferences(client, home, lat=0.018, lon=0, sports=[("running", 3)])
    put(client, me, 0.5)                                    # I report being ~55 km away from everyone
    assert cards(client, me, radius_km=5) == []
    from dataclasses import replace
    me_id = __import__("uuid").UUID(me["user_id"])
    fresh = repository._locations[me_id]
    repository._locations[me_id] = replace(fresh, actualizada_en=fresh.actualizada_en - timedelta(days=2))
    assert [c["user_id"] for c in cards(client, me, radius_km=5)] == [home["user_id"]]  # back to my profile zone
