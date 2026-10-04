from test_suggestions import make_client, signed_up


def test_directory_is_public_only_and_includes_self_for_matching_validation():
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    other = signed_up(client, "other@example.com")
    response = client.post("/api/v1/users/athletes/cards", headers=me["headers"], json=[me["user_id"], other["user_id"]])
    assert response.status_code == 200
    assert {card["user_id"] for card in response.json()} == {me["user_id"], other["user_id"]}
    assert all("email" not in card and "rut" not in card and "zona" not in card for card in response.json())


def test_directory_excludes_clubs_and_requires_an_active_verified_athlete():
    client, repo = make_client()
    me = signed_up(client, "me@example.com")
    club = signed_up(client, "club@example.com")
    repo.get_user_by_email("club@example.com").role = "club_admin"
    path = "/api/v1/users/athletes/" + club["user_id"]
    assert client.get(path, headers=me["headers"]).status_code == 404
    assert client.get("/api/v1/users/athletes/" + me["user_id"], headers=club["headers"]).status_code == 403
    repo.get_user_by_email("me@example.com").email_verified = False
    assert client.get("/api/v1/users/athletes/" + me["user_id"], headers=me["headers"]).status_code == 403


def test_directory_requires_token_and_limits_batch_size():
    client, _ = make_client()
    me = signed_up(client, "me@example.com")
    assert client.get("/api/v1/users/athletes/" + me["user_id"]).status_code == 401
    assert client.post("/api/v1/users/athletes/cards", headers=me["headers"], json=[me["user_id"]] * 101).status_code == 422
