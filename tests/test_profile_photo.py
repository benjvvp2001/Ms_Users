"""HU-61: guardar la foto de perfil en el backend (repositorio simulado y carpeta temporal, sin base de datos)."""
from __future__ import annotations

import io
import os
import re

os.environ["JWT_SECRET"] = "test-only-secret-that-is-long-enough"

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.repositories.mock_user_repository import MockUserRepository
from app.storage.photos import LocalPhotoStorage, PhotoStorageError, is_photo_key, photo_url
from fakes import FakeEmailSender, register_and_verify

USERS = "/api/v1/users"
PASSWORD = "A-secure-test-password-1"
KEY_URL = re.compile(r"^/api/v1/users/photos/[0-9a-f]{32}\.(jpg|png)$")


# -- helpers --------------------------------------------------------------------------------------


def jpeg(size=(64, 48), color=(200, 30, 30), exif=None, **extra) -> bytes:
    out = io.BytesIO()
    kwargs = dict(extra)
    if exif is not None:
        kwargs["exif"] = exif
    Image.new("RGB", size, color).save(out, format="JPEG", **kwargs)
    return out.getvalue()


def png(size=(40, 40), mode="RGB", color=(10, 120, 200), text=None) -> bytes:
    from PIL.PngImagePlugin import PngInfo
    out = io.BytesIO()
    info = None
    if text:
        info = PngInfo()
        info.add_text("Author", text)
    Image.new(mode, size, color if mode == "RGB" else color + (128,)).save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def gps_exif() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    exif[0x8825] = {1: "S", 2: (33.0, 26.0, 30.0), 3: "W", 4: (70.0, 38.0, 55.0)}  # GPS IFD
    return exif.tobytes()


def upload(client, who, data, filename="foto.jpg", content_type="image/jpeg", field="foto", headers=None):
    return client.put(f"{USERS}/{who['id']}/profile/photo", headers=headers or who["headers"],
                      files={field: (filename, data, content_type)})


@pytest.fixture
def storage(tmp_path):
    return LocalPhotoStorage(tmp_path / "photos")


@pytest.fixture
def client(storage):
    return TestClient(create_app(repository=MockUserRepository(), email_sender=FakeEmailSender(), photo_storage=storage))


def person(client, name):
    tokens = register_and_verify(client, {"email": f"{name.lower()}@example.com", "password": PASSWORD,
                                          "nombre": name, "apellido_paterno": "Torres"})
    return {"id": tokens["user"]["user_id"], "headers": {"Authorization": f"Bearer {tokens['access_token']}"}}


@pytest.fixture
def ana(client):
    return person(client, "Ana")


@pytest.fixture
def beto(client):
    return person(client, "Beto")


def profile(client, who, of=None):
    return client.get(f"{USERS}/{(of or who)['id']}/profile", headers=who["headers"])


def stored_files(storage):
    return storage.keys()


# -- subir ----------------------------------------------------------------------------------------


def test_a_valid_jpg_is_stored_and_the_profile_returns_its_url(client, ana, storage):
    response = upload(client, ana, jpeg())
    assert response.status_code == 200, response.text
    url = response.json()["foto_perfil"]
    assert KEY_URL.match(url) and url.endswith(".jpg")
    assert profile(client, ana).json()["foto_perfil"] == url          # still there when the person comes back
    assert stored_files(storage) == [url.rsplit("/", 1)[1]]


def test_a_valid_png_is_stored_as_png(client, ana, storage):
    url = upload(client, ana, png(), "avatar.png", "image/png").json()["foto_perfil"]
    assert url.endswith(".png")
    served = client.get(url)
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(served.content)).format == "PNG"


def test_the_stored_name_is_random_and_never_the_original_one(client, ana, storage):
    first = upload(client, ana, jpeg(), "mi-foto-privada.jpg").json()["foto_perfil"]
    second = upload(client, ana, jpeg(), "mi-foto-privada.jpg").json()["foto_perfil"]
    assert "privada" not in first and first != second


def test_a_path_traversal_file_name_cannot_write_outside_the_folder(client, ana, storage, tmp_path):
    response = upload(client, ana, jpeg(), "../../../etc/passwd.jpg")
    assert response.status_code == 200
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == stored_files(storage)
    assert all(is_photo_key(name) for name in stored_files(storage))


def test_exif_and_gps_are_removed_but_the_orientation_is_applied(client, ana):
    original = jpeg(size=(80, 40), exif=gps_exif())
    assert b"SecretCameraMaker" in original
    url = upload(client, ana, original).json()["foto_perfil"]
    body = client.get(url).content
    assert b"SecretCameraMaker" not in body and not Image.open(io.BytesIO(body)).getexif()
    # a photo the camera stored sideways (orientation 6) is rotated before the tag is dropped
    exif = Image.Exif()
    exif[0x0112] = 6
    sideways = Image.open(io.BytesIO(client.get(upload(client, ana, jpeg(size=(80, 40), exif=exif.tobytes())).json()["foto_perfil"]).content))
    assert sideways.size == (40, 80)


def test_png_text_chunks_and_trailing_data_are_dropped(client, ana):
    hidden = png(text="SECRET-NOTE") + b"<script>alert(1)</script>"
    body = client.get(upload(client, ana, hidden, "a.png", "image/png").json()["foto_perfil"]).content
    assert b"SECRET-NOTE" not in body and b"<script>" not in body


def test_a_large_picture_is_scaled_down_and_a_small_one_is_never_enlarged(client, ana, monkeypatch):
    monkeypatch.setenv("PHOTO_MAX_SIDE", "100")
    big = Image.open(io.BytesIO(client.get(upload(client, ana, jpeg(size=(400, 200))).json()["foto_perfil"]).content))
    assert big.size == (100, 50)
    small = Image.open(io.BytesIO(client.get(upload(client, ana, jpeg(size=(30, 20))).json()["foto_perfil"]).content))
    assert small.size == (30, 20)


def test_a_transparent_png_keeps_its_transparency(client, ana):
    body = client.get(upload(client, ana, png(mode="RGBA"), "a.png", "image/png").json()["foto_perfil"]).content
    assert Image.open(io.BytesIO(body)).mode == "RGBA"


# -- rechazos -------------------------------------------------------------------------------------


@pytest.mark.parametrize("data,name,declared,status", [
    (b"MZ\x90\x00" + b"\x00" * 200, "foto.jpg", "image/jpeg", 415),                       # an .exe renamed .jpg
    (b"<html><script>alert(1)</script></html>", "foto.jpg", "image/jpeg", 415),              # an .html renamed .jpg
    (b"GIF89a" + b"\x00" * 50, "foto.jpg", "image/jpeg", 415),                                 # a real image, but a GIF
    (b"%PDF-1.4 hello", "foto.png", "image/png", 415),
    (jpeg(), "foto.gif", "image/jpeg", 415),                                                   # extension not allowed
    (jpeg(), "foto", "image/jpeg", 415),                                                       # no extension at all
    (jpeg(), "foto.jpg", "text/html", 415),                                                    # declared type not an image
    (jpeg(), "foto.php.jpg", "image/jpeg", 400),                                               # double extension
    (jpeg(), "foto.exe.png", "image/png", 400),
    (jpeg(), "foto.html.jpeg", "image/jpeg", 400),
    (b"", "foto.jpg", "image/jpeg", 400),                                                      # empty
    (jpeg()[:60], "foto.jpg", "image/jpeg", 400),                                              # truncated / corrupt
    (b"\xff\xd8\xff" + b"garbage" * 40, "foto.jpg", "image/jpeg", 400),                       # signature only
    (png()[:50], "foto.png", "image/png", 400),
])
def test_a_bad_file_is_refused_with_a_clear_error_and_nothing_is_stored(client, ana, storage, data, name, declared, status):
    response = upload(client, ana, data, name, declared)
    assert response.status_code == status, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]
    assert stored_files(storage) == []
    assert profile(client, ana).json()["foto_perfil"] is None


def test_a_png_sent_with_a_jpg_name_is_accepted_because_the_content_decides(client, ana):
    url = upload(client, ana, png(), "foto.jpg", "image/jpeg").json()["foto_perfil"]
    assert url.endswith(".png")


def test_a_photo_over_the_limit_is_413(client, ana, storage, monkeypatch):
    monkeypatch.setenv("PHOTO_MAX_BYTES", "20000")
    noisy = io.BytesIO()
    Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3)).save(noisy, format="PNG")
    assert len(noisy.getvalue()) > 20000
    response = upload(client, ana, noisy.getvalue(), "a.png", "image/png")
    assert response.status_code == 413 and "tamaño" in response.json()["detail"]
    assert stored_files(storage) == []


def test_the_declared_content_length_is_checked_before_reading_the_body(client, ana, monkeypatch):
    monkeypatch.setenv("PHOTO_MAX_BYTES", "1000")
    response = client.put(f"{USERS}/{ana['id']}/profile/photo", headers={**ana["headers"], "Content-Length": "999999999",
                          "Content-Type": "multipart/form-data; boundary=x"}, content=b"--x--")
    assert response.status_code == 413


def test_an_image_with_too_many_pixels_is_413(client, ana, monkeypatch):
    monkeypatch.setenv("PHOTO_MAX_PIXELS", "1000")
    assert upload(client, ana, jpeg(size=(100, 100))).status_code == 413


def test_a_request_without_the_file_field_or_not_multipart_is_a_clear_4xx(client, ana):
    assert upload(client, ana, jpeg(), field="otro").status_code == 400
    wrong = client.put(f"{USERS}/{ana['id']}/profile/photo", headers=ana["headers"], json={"foto": "x"})
    assert wrong.status_code == 415
    garbage = client.put(f"{USERS}/{ana['id']}/profile/photo", content=b"nonsense",
                         headers={**ana["headers"], "Content-Type": "multipart/form-data; boundary=zzz"})
    assert garbage.status_code in (400, 422)


# -- sesión y cuenta propia -----------------------------------------------------------------------


def test_uploading_without_a_session_is_401(client, ana, storage):
    assert client.put(f"{USERS}/{ana['id']}/profile/photo", files={"foto": ("a.jpg", jpeg(), "image/jpeg")}).status_code == 401
    assert client.delete(f"{USERS}/{ana['id']}/profile/photo").status_code == 401
    assert client.put(f"{USERS}/{ana['id']}/profile/photo", headers={"Authorization": "Bearer garbage"},
                      files={"foto": ("a.jpg", jpeg(), "image/jpeg")}).status_code == 401
    assert stored_files(storage) == []


def test_nobody_can_change_another_persons_photo(client, ana, beto, storage):
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    attempt = client.put(f"{USERS}/{ana['id']}/profile/photo", headers=beto["headers"],
                         files={"foto": ("a.jpg", jpeg(), "image/jpeg")})
    assert attempt.status_code == 403
    assert client.delete(f"{USERS}/{ana['id']}/profile/photo", headers=beto["headers"]).status_code == 403
    assert profile(client, ana).json()["foto_perfil"] == url and len(stored_files(storage)) == 1


def test_not_even_an_administrator_can_change_somebody_elses_photo(storage):
    repository = MockUserRepository()
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender(), photo_storage=storage))
    ana = person(client, "Ana")
    admin = person(client, "Admin")
    repository._users[__import__("uuid").UUID(admin["id"])].role = "admin"
    assert upload(client, ana, jpeg(), headers=admin["headers"]).status_code == 403
    assert stored_files(storage) == []


# -- reemplazar y borrar --------------------------------------------------------------------------


def test_replacing_the_photo_deletes_the_previous_file(client, ana, storage):
    first = upload(client, ana, jpeg()).json()["foto_perfil"]
    second = upload(client, ana, png(), "a.png", "image/png").json()["foto_perfil"]
    assert first != second
    assert stored_files(storage) == [second.rsplit("/", 1)[1]]
    assert client.get(first).status_code == 404 and client.get(second).status_code == 200


def test_deleting_the_photo_removes_the_file_and_leaves_no_photo(client, ana, storage):
    upload(client, ana, jpeg())
    response = client.delete(f"{USERS}/{ana['id']}/profile/photo", headers=ana["headers"])
    assert response.status_code == 200 and response.json()["foto_perfil"] is None
    assert stored_files(storage) == []
    again = client.delete(f"{USERS}/{ana['id']}/profile/photo", headers=ana["headers"])    # idempotent
    assert again.status_code == 200 and again.json()["foto_perfil"] is None


def test_deleting_the_account_deletes_the_photo_file(client, ana, storage):
    upload(client, ana, jpeg())
    assert client.delete(f"{USERS}/{ana['id']}", headers=ana["headers"]).status_code == 204
    assert stored_files(storage) == []


# -- fallos del almacenamiento --------------------------------------------------------------------


class BrokenStorage(LocalPhotoStorage):
    def save(self, key, data):
        raise PhotoStorageError("disk full")


class SometimesBrokenRepository(MockUserRepository):
    fail = False

    def set_photo(self, user_id, key):
        if self.fail and key is not None:
            raise RuntimeError("database down")
        return super().set_photo(user_id, key)


def test_when_the_storage_fails_the_profile_keeps_its_previous_photo(tmp_path):
    good = LocalPhotoStorage(tmp_path / "p")
    app = create_app(repository=MockUserRepository(), email_sender=FakeEmailSender(), photo_storage=good)
    client = TestClient(app, raise_server_exceptions=False)
    ana = person(client, "Ana")
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    app.state.photo_storage = BrokenStorage(tmp_path / "p")
    response = upload(client, ana, jpeg())
    assert response.status_code == 503
    assert profile(client, ana).json()["foto_perfil"] == url
    assert client.get(url).status_code in (200, 503)
    assert good.keys() == [url.rsplit("/", 1)[1]]            # the old file is intact


def test_when_the_database_fails_the_new_file_is_removed_and_the_old_photo_stays(tmp_path):
    storage = LocalPhotoStorage(tmp_path / "p")
    repository = SometimesBrokenRepository()
    client = TestClient(create_app(repository=repository, email_sender=FakeEmailSender(), photo_storage=storage),
                        raise_server_exceptions=False)
    ana = person(client, "Ana")
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    repository.fail = True
    assert upload(client, ana, jpeg()).status_code == 500
    repository.fail = False
    assert storage.keys() == [url.rsplit("/", 1)[1]]
    assert profile(client, ana).json()["foto_perfil"] == url


def test_a_storage_that_cannot_delete_does_not_fail_the_request(tmp_path, monkeypatch):
    storage = LocalPhotoStorage(tmp_path / "p")
    client = TestClient(create_app(repository=MockUserRepository(), email_sender=FakeEmailSender(), photo_storage=storage))
    ana = person(client, "Ana")
    upload(client, ana, jpeg())
    monkeypatch.setattr("pathlib.Path.unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("no")))
    assert client.delete(f"{USERS}/{ana['id']}/profile/photo", headers=ana["headers"]).status_code == 200
    monkeypatch.undo()
    assert profile(client, ana).json()["foto_perfil"] is None


# -- ver la foto ----------------------------------------------------------------------------------


def test_a_photo_is_served_with_the_right_type_and_nosniff(client, ana):
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    served = client.get(url)                                    # no session needed (the key is the capability)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in served.headers["content-security-policy"]
    assert served.headers["cache-control"].startswith("private")


@pytest.mark.parametrize("key", ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "x.jpg", "0" * 32 + ".html",
                                 "0" * 32 + ".jpg.exe", "0" * 31 + ".jpg", ("0" * 32 + ".jpg").upper(), "profile",
                                 "0" * 32 + ".jpg%00.png"])
def test_a_key_that_is_not_one_we_generated_is_404(client, key):
    assert client.get(f"{USERS}/photos/{key}").status_code == 404


def test_a_well_formed_key_with_no_file_is_404(client):
    assert client.get(f"{USERS}/photos/{'a' * 32}.png").status_code == 404


def test_a_user_without_photo_returns_null(client, ana, beto):
    assert profile(client, ana).json()["foto_perfil"] is None
    cards = client.post(f"{USERS}/athletes/cards", headers=beto["headers"], json=[ana["id"]])
    assert cards.status_code == 200 and cards.json()[0]["foto_perfil"] is None


def test_other_people_see_the_url_in_their_cards_and_suggestions(client, ana, beto):
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    card = client.post(f"{USERS}/athletes/cards", headers=beto["headers"], json=[ana["id"]]).json()[0]
    assert card["foto_perfil"] == url
    assert client.get(f"{USERS}/athletes/{ana['id']}", headers=beto["headers"]).json()["foto_perfil"] == url
    suggestions = client.get(f"{USERS}/suggestions", headers=beto["headers"]).json()
    assert [s["foto_perfil"] for s in suggestions if s["user_id"] == ana["id"]] == [url]


def test_a_blocked_person_is_not_visible_so_neither_is_their_photo_url(client, ana, beto):
    upload(client, ana, jpeg())
    assert client.put(f"{USERS}/me/blocks/{ana['id']}", headers=beto["headers"]).status_code == 204
    assert client.post(f"{USERS}/athletes/cards", headers=beto["headers"], json=[ana["id"]]).json() == []
    assert client.get(f"{USERS}/athletes/{ana['id']}", headers=beto["headers"]).status_code == 404
    assert all(s["user_id"] != ana["id"] for s in client.get(f"{USERS}/suggestions", headers=beto["headers"]).json())
    # and the other way round: Ana does not get Beto's photo either
    upload(client, beto, jpeg())
    assert client.post(f"{USERS}/athletes/cards", headers=ana["headers"], json=[beto["id"]]).json() == []


def test_another_persons_full_profile_stays_private(client, ana, beto):
    upload(client, ana, jpeg())
    assert client.get(f"{USERS}/{ana['id']}/profile", headers=beto["headers"]).status_code == 403


def test_the_data_export_carries_the_url_too(client, ana):
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    export = client.get(f"{USERS}/{ana['id']}/exports", headers=ana["headers"]).json()
    assert export["profile"]["foto_perfil"] == url


# -- el perfil no deja escribir la foto -----------------------------------------------------------


def test_the_profile_update_cannot_set_or_clear_the_photo(client, ana, storage):
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    current = profile(client, ana).json()
    body = {k: current[k] for k in ("nombre", "apellido_paterno", "apellido_materno", "fecha_nacimiento", "telefono")}
    # what the app sends back after reading the profile (the URL it got), and an attempt to point anywhere else
    for attempt in (url, "http://evil.example/x.png", "file:///etc/passwd", None):
        response = client.put(f"{USERS}/{ana['id']}/profile", headers=ana["headers"],
                              json={**body, "biografia": "hola", "foto_perfil": attempt})
        assert response.status_code == 200
        assert response.json()["foto_perfil"] == url
    assert stored_files(storage) == [url.rsplit("/", 1)[1]]


def test_old_free_text_values_read_as_no_photo():
    assert photo_url("file:///data/user/0/cache/ImagePicker/abc.jpg") is None
    assert photo_url("https://example.com/a.png") is None
    assert photo_url(None) is None
    assert photo_url("a" * 32 + ".png") == "/api/v1/users/photos/" + "a" * 32 + ".png"


# -- sanciones (HU-45) y huérfanos ----------------------------------------------------------------


def test_a_suspended_account_cannot_upload_or_delete_its_photo(storage):
    from test_admin_audit import setup
    from test_sanctions import sanction
    client, repository, admin, ana = setup()
    client.app.state.photo_storage = storage
    assert upload(client, ana, jpeg()).status_code == 200
    assert sanction(client, admin, ana, tipo="permanente").status_code == 201
    assert upload(client, ana, jpeg()).status_code == 403
    response = client.delete(f"{USERS}/{ana['id']}/profile/photo", headers=ana["headers"])
    assert response.status_code == 403 and response.json()["code"] == "account_suspended"
    assert len(stored_files(storage)) == 1


def test_purging_orphans_removes_only_files_nobody_points_to(client, ana, storage):
    from app.storage.cleanup import purge_orphans
    url = upload(client, ana, jpeg()).json()["foto_perfil"]
    storage.save("b" * 32 + ".jpg", b"leftover")
    repository = client.app.state.repository
    assert purge_orphans(repository, storage, dry_run=True) == ["b" * 32 + ".jpg"]
    assert "b" * 32 + ".jpg" in storage.keys()
    assert purge_orphans(repository, storage) == ["b" * 32 + ".jpg"]
    assert storage.keys() == [url.rsplit("/", 1)[1]]
