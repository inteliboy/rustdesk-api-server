"""A shared address book's connection password, encrypted at rest.

The client sends a shared book's `password` in clear (`changeSharedPassword` /
`addIdToCurrent` in flutter/lib/models/ab_model.dart) and reads it back from the
peer JSON (`Peer.fromJson`, `password`) - so the server has to be able to return
it. It is therefore stored only encrypted, with DATA_ENCRYPTION_KEY, and only
when that key is set.
"""

import json

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from rustdesk_api.cli import main
from rustdesk_api.config import Settings, clear_settings_cache
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.security.encryption import generate_key

PASSWORD = "alicepassword1"
SECRET = "hunter2-connection-pw"


@pytest.fixture()
def data_key(monkeypatch):
    """Set before `admin_client` (fixture order), so the app is built with it."""
    key = generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", key)
    clear_settings_cache()
    return key


def _login(client, username="admin", password="adminpass123"):
    r = client.post("/api/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _book(admin_client, name="Team"):
    r = admin_client.post("/api/v1/address-books", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["guid"]


def _peers(client, headers, guid):
    r = client.post("/api/ab/peers", params={"ab": guid, "current": 1, "pageSize": 100}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _stored_tokens():
    with get_session_factory()() as db:
        return [
            row[0]
            for row in db.execute(text("SELECT password_enc FROM address_book_entries")).all()
            if row[0] is not None
        ]


def _add(client, headers, guid, peer):
    r = client.post(f"/api/ab/peer/add/{guid}", json=peer, headers=headers)
    assert (r.status_code, r.content) == (200, b""), r.text


def _update(client, headers, guid, peer):
    r = client.put(f"/api/ab/peer/update/{guid}", json=peer, headers=headers)
    assert (r.status_code, r.content) == (200, b""), r.text


def test_the_password_round_trips_and_is_stored_encrypted(data_key, admin_client):
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "alias": "srv", "password": SECRET})

    peer = _peers(admin_client, headers, guid)[0]
    assert peer["password"] == SECRET
    assert "hash" not in peer

    (token,) = _stored_tokens()
    assert SECRET not in token
    assert token.startswith("gAAAA")  # a Fernet token


def test_update_changes_keeps_and_clears_it(data_key, admin_client):
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET})

    _update(admin_client, headers, guid, {"id": "111", "password": "second-pw"})
    assert _peers(admin_client, headers, guid)[0]["password"] == "second-pw"

    # An update without the key leaves it alone (e.g. a tag or alias edit).
    _update(admin_client, headers, guid, {"id": "111", "alias": "renamed"})
    assert _peers(admin_client, headers, guid)[0]["password"] == "second-pw"

    # The client clears a password by sending an empty one.
    _update(admin_client, headers, guid, {"id": "111", "password": ""})
    assert "password" not in _peers(admin_client, headers, guid)[0]
    assert _stored_tokens() == []


def test_it_never_reaches_the_management_api_or_the_logs(data_key, admin_client, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET})

    body = json.dumps(admin_client.get(f"/api/v1/address-book?book={guid}").json())
    assert SECRET not in body
    assert '"has_password": true' in body
    assert SECRET not in caplog.text


def test_a_personal_book_ignores_a_password(data_key, admin_client):
    headers = _login(admin_client)
    guid = admin_client.post("/api/ab/personal", headers=headers).json()["guid"]
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET, "hash": "h"})

    peer = _peers(admin_client, headers, guid)[0]
    assert peer["hash"] == "h"
    assert "password" not in peer
    assert _stored_tokens() == []


def test_without_a_key_the_password_is_accepted_and_dropped(admin_client):
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET})

    assert "password" not in _peers(admin_client, headers, guid)[0]
    assert _stored_tokens() == []


def test_a_sharee_sees_it_only_with_access(data_key, admin_client):
    """Returning the password to everyone who can read the book is the point of
    a shared book with saved passwords; someone without a share gets a 404."""
    guid = _book(admin_client)
    headers = _login(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET})
    for name in ("reader", "outsider"):
        r = admin_client.post(
            "/api/v1/users", json={"username": name, "password": PASSWORD, "is_admin": False}
        )
        assert r.status_code == 201
    r = admin_client.put(f"/api/v1/address-books/{guid}/shares", json={"username": "reader", "rule": 1})
    assert r.status_code == 200

    assert _peers(admin_client, _login(admin_client, "reader", PASSWORD), guid)[0]["password"] == SECRET

    r = admin_client.post(
        "/api/ab/peers",
        params={"ab": guid, "current": 1, "pageSize": 100},
        headers=_login(admin_client, "outsider", PASSWORD),
    )
    assert r.status_code == 404
    assert SECRET not in r.text

    # Read-only cannot change it.
    r = admin_client.put(
        f"/api/ab/peer/update/{guid}",
        json={"id": "111", "password": "x"},
        headers=_login(admin_client, "reader", PASSWORD),
    )
    assert r.status_code == 403
    assert _peers(admin_client, headers, guid)[0]["password"] == SECRET


def test_an_unusable_password_is_a_client_error(data_key, admin_client):
    headers = _login(admin_client)
    guid = _book(admin_client)
    r = admin_client.post(
        f"/api/ab/peer/add/{guid}", json={"id": "1", "password": "x" * 257}, headers=headers
    )
    assert r.status_code == 422
    r = admin_client.post(f"/api/ab/peer/add/{guid}", json={"id": "2", "password": 12345}, headers=headers)
    assert r.status_code == 422


def test_a_lost_key_degrades_to_no_password_without_an_error(data_key, admin_client, monkeypatch, caplog):
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "alias": "srv", "password": SECRET})
    (token,) = _stored_tokens()

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", generate_key())
    clear_settings_cache()
    peer = _peers(admin_client, headers, guid)[0]
    assert peer["alias"] == "srv"
    assert "password" not in peer
    assert "cannot decrypt" in caplog.text.lower()
    assert SECRET not in caplog.text
    assert _stored_tokens() == [token]  # not destroyed: the right key still opens it


def test_rotation_reencrypts_under_the_new_key(data_key, admin_client, monkeypatch):
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET})
    (old_token,) = _stored_tokens()

    new_key = generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", f"{new_key},{data_key}")
    clear_settings_cache()
    assert _peers(admin_client, headers, guid)[0]["password"] == SECRET  # old key still reads it

    result = CliRunner().invoke(main, ["rotate-data-key"])
    assert result.exit_code == 0, result.output
    assert "1 stored password" in result.output
    (new_token,) = _stored_tokens()
    assert new_token != old_token

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", new_key)  # old key dropped
    clear_settings_cache()
    assert _peers(admin_client, headers, guid)[0]["password"] == SECRET


def test_rotation_reports_rows_it_cannot_open(data_key, admin_client, monkeypatch):
    headers = _login(admin_client)
    guid = _book(admin_client)
    _add(admin_client, headers, guid, {"id": "111", "password": SECRET})
    (token,) = _stored_tokens()

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", generate_key())
    clear_settings_cache()
    result = CliRunner().invoke(main, ["rotate-data-key"])
    assert result.exit_code == 1
    assert "could not be decrypted" in result.output
    assert _stored_tokens() == [token]


def test_rotation_needs_a_key(admin_client):
    result = CliRunner().invoke(main, ["rotate-data-key"])
    assert result.exit_code != 0
    assert "DATA_ENCRYPTION_KEY is not set" in result.output


def test_generate_key_prints_a_usable_key(monkeypatch):
    key = CliRunner().invoke(main, ["generate-key"]).output.strip()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", key)
    clear_settings_cache()
    assert Settings().data_encryption_key == key


def test_a_malformed_key_is_refused_without_echoing_it(monkeypatch):
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "not-a-real-key-value")
    clear_settings_cache()
    with pytest.raises(ValueError) as excinfo:
        Settings()
    assert "not-a-real-key-value" not in str(excinfo.value)


def test_check_config_redacts_the_key(data_key, settings):
    out = CliRunner().invoke(main, ["check-config"]).output
    assert data_key not in out
    assert "DATA_ENCRYPTION_KEY is not set" not in out


def test_check_config_notes_a_missing_key(settings):
    out = CliRunner().invoke(main, ["check-config"]).output
    assert "DATA_ENCRYPTION_KEY is not set" in out
