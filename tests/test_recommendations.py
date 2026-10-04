from app.core.recommendations import distance_km
from app.schemas.user import Zona
from test_suggestions import make_client, signed_up, SUGGESTIONS_PATH


def preferences(client, user, *, lat=None, lon=None, sports=None, available=True):
    body = {"deportes": [{"deporte_codigo": code, "nivel": level} for code, level in (sports or [])],
            "disponibilidad_match": available}
    if lat is not None:
        body["zona"] = {"comuna": "Santiago", "latitud": lat, "longitud": lon}
    response = client.put(f"/api/v1/users/{user['user_id']}/preferences", headers=user["headers"], json=body)
    assert response.status_code == 200, response.text


def cards(client, user, **params):
    response = client.get(SUGGESTIONS_PATH, headers=user["headers"], params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_radius_excludes_unknown_locations_and_orders_nearest_first():
    client, _ = make_client()
    me = signed_up(client, 'me@example.com')
    preferences(client, me, lat=0, lon=0, sports=[('running', 3)])
    # Around 2, 7 and 12 km from the equator. Newest is the farthest.
    near = signed_up(client, 'near@example.com')
    preferences(client, near, lat=0.018, lon=0, sports=[('running', 4)])
    middle = signed_up(client, 'middle@example.com')
    preferences(client, middle, lat=0.063, lon=0, sports=[('running', 3)])
    far = signed_up(client, 'far@example.com')
    preferences(client, far, lat=0.108, lon=0, sports=[('running', 3)])
    unknown = signed_up(client, 'unknown@example.com')
    preferences(client, unknown, sports=[('running', 3)])
    assert [c['user_id'] for c in cards(client, me, radius_km=5)] == [near['user_id']]
    ten = cards(client, me, radius_km=10)
    assert [c['user_id'] for c in ten] == [near['user_id'], middle['user_id']]
    assert [c['distancia_km'] for c in ten] == [2.0, 7.0]
    assert [c['user_id'] for c in cards(client, me)] == [near['user_id'], middle['user_id'], far['user_id'], unknown['user_id']]
    assert 'latitud' not in str(ten) and 'longitud' not in str(ten)
    assert cards(client, me, radius_km=10, limit=1)[0]['user_id'] == near['user_id']


def test_level_and_sport_conditions_must_match_the_same_sport():
    client, _ = make_client()
    me = signed_up(client, 'me@example.com')
    preferences(client, me, sports=[('running', 3)])
    mixed = signed_up(client, 'mixed@example.com')
    preferences(client, mixed, sports=[('running', 5), ('tennis', 2)])
    same = signed_up(client, 'same@example.com')
    preferences(client, same, sports=[('RUNNING', 4)])
    assert [c['user_id'] for c in cards(client, me, sport='running', min_level=2, max_level=4)] == [same['user_id']]
    assert [c['user_id'] for c in cards(client, me, shared_sports=True, level_tolerance=1)] == [same['user_id']]
    assert len(cards(client, me, shared_sports=True)) == 2
    assert cards(client, me, sport='tennis', level_tolerance=1) == []
    assert cards(client, me, sport='tennis', min_level=1, max_level=2)[0]['user_id'] == mixed['user_id']


def test_without_coordinates_radius_errors_and_no_radius_still_works():
    client, _ = make_client()
    me = signed_up(client, 'me@example.com')
    other = signed_up(client, 'other@example.com')
    response = client.get(SUGGESTIONS_PATH, headers=me['headers'], params={'radius_km': 10})
    assert response.status_code == 422
    assert 'ubicación' in response.json()['detail']
    assert cards(client, me)[0]['user_id'] == other['user_id']
    assert cards(client, me)[0]['distancia_km'] is None
    assert cards(client, me, shared_sports=True) == []


def test_invalid_filter_ranges_and_hidden_matching_preference():
    client, _ = make_client()
    me = signed_up(client, 'me@example.com')
    other = signed_up(client, 'other@example.com')
    preferences(client, other, available=False)
    assert cards(client, me) == []
    for query in ({'radius_km': -1}, {'radius_km': 'nan'}, {'radius_km': 101}, {'min_level': 0},
                  {'min_level': 5, 'max_level': 2}, {'max_level': 6}, {'level_tolerance': -1}):
        assert client.get(SUGGESTIONS_PATH, headers=me['headers'], params=query).status_code == 422


def test_compatibility_accounts_for_level_and_handles_zero_coordinates():
    client, _ = make_client()
    me = signed_up(client, 'me@example.com')
    preferences(client, me, sports=[('running', 3)])
    same = signed_up(client, 'same@example.com')
    preferences(client, same, sports=[('running', 3)])
    different = signed_up(client, 'different@example.com')
    preferences(client, different, sports=[('running', 5)])
    results = cards(client, me)
    assert [(c['user_id'], c['compatibilidad'], c['nivel_coincidente']) for c in results] == [
        (same['user_id'], 100, True), (different['user_id'], 50, False)]
    equator = Zona(comuna='Prueba', latitud=0, longitud=0)
    assert distance_km(equator, equator) == 0
    assert 20010 < distance_km(equator, Zona(comuna='Prueba', latitud=0, longitud=180)) < 20020
