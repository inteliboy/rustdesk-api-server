"""Two-factor authentication, on both sign-in paths.

The RustDesk client's part is its own protocol (`hbbs.dart`, `login.dart`): the
first `/api/login` answers `type: email_check` + `tfa_type: tfa_check` with a
`secret`; the client asks for the code and repeats the login with
`type: email_code`, `secret`, `tfaCode` and the username (no password).
"""

from __future__ import annotations

import json
import types

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import text

from rustdesk_api.cli import main
from rustdesk_api.config import clear_settings_cache
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.security import totp
from rustdesk_api.security.encryption import generate_key

PASSWORD = "adminpass123"
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    """A data key (2FA needs one), a generous login limiter (every request comes
    from one IP), and a frozen clock for the code arithmetic."""
    key = generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("AUTH_RATE_LIMIT_ATTEMPTS", "1000")
    clear_settings_cache()
    _clock(monkeypatch, NOW)
    return key


def _clock(monkeypatch, now):
    """Freeze the time the TOTP code uses (only that module's view of it)."""
    monkeypatch.setattr(totp, "time", types.SimpleNamespace(time=lambda: now))


_state: dict = {}


@pytest.fixture(autouse=True)
def _remember_app(_environment, app):
    _state["app"] = app


def _setup(client):
    r = client.post("/api/v1/auth/2fa/setup")
    assert r.status_code == 200, r.text
    return r.json()["secret"]


def _enable(client, at=NOW):
    secret = _setup(client)
    r = client.post("/api/v1/auth/2fa/enable", json={"code": totp.code_at(secret, at)})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


def _fresh():
    """A client with no cookies, against the same app and database."""
    return TestClient(_state["app"])


def _web_login(client, username="admin", password=PASSWORD):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def _client_login(client, **body):
    return client.post("/api/login", json={"username": "admin", "password": PASSWORD, "id": "1001", **body})


def _audit(client):
    return client.get("/api/v1/admin/audit-logs?page_size=100").json()["items"]


def _user(app, admin_client, name):
    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201
    other = TestClient(app)
    other.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    other.headers.update({"X-CSRF-Token": other.cookies.get("rd_csrf")})
    return other, r.json()["id"]


# --------------------------------------------------------------------------
# Enrolment
# --------------------------------------------------------------------------


def test_status_reports_whether_the_server_can_do_it(admin_client):
    assert admin_client.get("/api/v1/auth/2fa").json() == {
        "enabled": False,
        "available": True,
        "recovery_codes_remaining": 0,
    }


def test_setup_needs_a_data_encryption_key(admin_client, monkeypatch):
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "")
    clear_settings_cache()
    assert admin_client.get("/api/v1/auth/2fa").json()["available"] is False
    r = admin_client.post("/api/v1/auth/2fa/setup")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "TWO_FACTOR_UNAVAILABLE")


def test_enrolment_needs_a_correct_code_and_stores_the_secret_encrypted(admin_client):
    r = admin_client.post("/api/v1/auth/2fa/setup")
    body = r.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/") and body["secret"] in body["otpauth_uri"]

    r = admin_client.post("/api/v1/auth/2fa/enable", json={"code": "000000"})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "INVALID_CODE")
    assert admin_client.get("/api/v1/auth/2fa").json()["enabled"] is False

    r = admin_client.post("/api/v1/auth/2fa/enable", json={"code": totp.code_at(body["secret"], NOW)})
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert admin_client.get("/api/v1/auth/2fa").json() == {
        "enabled": True,
        "available": True,
        "recovery_codes_remaining": 10,
    }

    with get_session_factory()() as db:
        row = db.execute(text("SELECT totp_secret_enc FROM users WHERE username='admin'")).scalar_one()
        stored_codes = [r[0] for r in db.execute(text("SELECT code_hash FROM recovery_codes")).all()]
    assert body["secret"] not in row and row.startswith("gAAAA")  # a Fernet token
    assert not set(codes) & set(stored_codes)  # only hashes are kept


def test_enabling_without_starting_setup_or_twice_is_refused(admin_client):
    r = admin_client.post("/api/v1/auth/2fa/enable", json={"code": "123456"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "TWO_FACTOR_NOT_ON")
    _enable(admin_client)
    assert admin_client.post("/api/v1/auth/2fa/setup").status_code == 409
    r = admin_client.post("/api/v1/auth/2fa/enable", json={"code": "123456"})
    assert r.status_code == 409


def test_setup_is_for_signed_in_users_only(client):
    assert client.post("/api/v1/auth/2fa/setup").status_code in (401, 403)
    assert client.get("/api/v1/auth/2fa").status_code == 401


# --------------------------------------------------------------------------
# WebUI sign-in
# --------------------------------------------------------------------------


def test_a_password_alone_no_longer_signs_in(admin_client):
    _enable(admin_client)
    web = _fresh()
    r = _web_login(web)
    assert r.status_code == 200
    body = r.json()
    assert body["two_factor_required"] is True and body["challenge"]
    assert body["access_token"] is None and body["user"] is None
    assert "rd_session" not in web.cookies
    assert web.get("/api/v1/auth/me").status_code == 401


def test_the_code_completes_the_sign_in(admin_client):
    secret, _ = _enable(admin_client)
    web = _fresh()
    challenge = _web_login(web).json()["challenge"]

    later = NOW + 30  # the step after the one used to enable
    r = web.post("/api/v1/auth/login/2fa", json={"challenge": challenge, "code": totp.code_at(secret, later)})
    assert r.status_code == 200 and r.json()["user"]["two_factor_enabled"] is True
    assert "rd_session" in web.cookies and web.get("/api/v1/auth/me").status_code == 200


def test_a_wrong_code_is_refused_and_too_many_kill_the_challenge(admin_client):
    secret, _ = _enable(admin_client)
    web = _fresh()
    challenge = _web_login(web).json()["challenge"]
    good = totp.code_at(secret, NOW + 30)

    for _ in range(4):
        r = web.post("/api/v1/auth/login/2fa", json={"challenge": challenge, "code": "000000"})
        assert (r.status_code, r.json()["error"]["code"]) == (401, "INVALID_CODE")
    r = web.post("/api/v1/auth/login/2fa", json={"challenge": challenge, "code": "000000"})
    assert r.status_code == 401
    # The fifth failure spent the challenge: even the right code cannot use it now.
    r = web.post("/api/v1/auth/login/2fa", json={"challenge": challenge, "code": good})
    assert r.status_code == 401
    assert "rd_session" not in web.cookies


def test_a_code_cannot_be_used_twice(admin_client, monkeypatch):
    secret, _ = _enable(admin_client)
    _clock(monkeypatch, NOW + 30)
    code = totp.code_at(secret, NOW + 30)

    first = _fresh()
    r = first.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(first).json()["challenge"], "code": code}
    )
    assert r.status_code == 200

    second = _fresh()
    r = second.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(second).json()["challenge"], "code": code}
    )
    assert r.status_code == 401  # same code, same step

    # A code from the code's own step is refused too, but the next step is fine.
    _clock(monkeypatch, NOW + 60)
    third = _fresh()
    r = third.post(
        "/api/v1/auth/login/2fa",
        json={"challenge": _web_login(third).json()["challenge"], "code": totp.code_at(secret, NOW + 60)},
    )
    assert r.status_code == 200


def test_the_enabling_code_cannot_be_replayed_at_sign_in(admin_client):
    secret, _ = _enable(admin_client)
    web = _fresh()
    r = web.post(
        "/api/v1/auth/login/2fa",
        json={"challenge": _web_login(web).json()["challenge"], "code": totp.code_at(secret, NOW)},
    )
    assert r.status_code == 401


def test_a_challenge_is_single_use_and_expires(admin_client, monkeypatch):
    secret, _ = _enable(admin_client)
    web = _fresh()
    challenge = _web_login(web).json()["challenge"]
    code = totp.code_at(secret, NOW + 30)
    assert web.post("/api/v1/auth/login/2fa", json={"challenge": challenge, "code": code}).status_code == 200
    other = _fresh()
    assert (
        other.post(
            "/api/v1/auth/login/2fa", json={"challenge": challenge, "code": totp.code_at(secret, NOW + 60)}
        ).status_code
        == 401
    )

    expired = _web_login(_fresh()).json()["challenge"]
    with get_session_factory()() as db:
        db.execute(text("UPDATE login_challenges SET expires_at = '2000-01-01 00:00:00'"))
        db.commit()
    _clock(monkeypatch, NOW + 90)  # a code that is otherwise good: only the expiry is at fault
    r = _fresh().post(
        "/api/v1/auth/login/2fa", json={"challenge": expired, "code": totp.code_at(secret, NOW + 90)}
    )
    assert r.status_code == 401


def test_a_made_up_challenge_gets_nowhere(client):
    r = client.post("/api/v1/auth/login/2fa", json={"challenge": "x" * 40, "code": "123456"})
    assert r.status_code == 401


def test_a_recovery_code_works_once(admin_client):
    _, codes = _enable(admin_client)
    web = _fresh()
    r = web.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(web).json()["challenge"], "code": codes[0]}
    )
    assert r.status_code == 200

    again = _fresh()
    r = again.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(again).json()["challenge"], "code": codes[0]}
    )
    assert r.status_code == 401
    # The formatting is forgiving: case, dashes and spaces.
    third = _fresh()
    sloppy = codes[1].upper().replace("-", " ")
    r = third.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(third).json()["challenge"], "code": sloppy}
    )
    assert r.status_code == 200
    assert web.get("/api/v1/auth/2fa").json()["recovery_codes_remaining"] == 8


def test_regenerating_recovery_codes_needs_the_password_and_a_code(admin_client):
    secret, old = _enable(admin_client)
    good = totp.code_at(secret, NOW + 30)

    r = admin_client.post(
        "/api/v1/auth/2fa/recovery-codes", json={"password": "wrong-password", "code": good}
    )
    assert r.status_code == 403
    r = admin_client.post("/api/v1/auth/2fa/recovery-codes", json={"password": PASSWORD, "code": "000000"})
    assert r.status_code == 403

    r = admin_client.post("/api/v1/auth/2fa/recovery-codes", json={"password": PASSWORD, "code": good})
    assert r.status_code == 200
    new = r.json()["recovery_codes"]
    assert len(new) == 10 and not set(new) & set(old)

    web = _fresh()
    r = web.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(web).json()["challenge"], "code": old[0]}
    )
    assert r.status_code == 401  # the old set is gone


def test_turning_it_off_needs_the_password_and_a_code(admin_client, monkeypatch):
    secret, _ = _enable(admin_client)
    _clock(monkeypatch, NOW + 30)
    good = totp.code_at(secret, NOW + 30)

    r = admin_client.post("/api/v1/auth/2fa/disable", json={"password": "wrong-password", "code": good})
    assert r.status_code == 403
    r = admin_client.post("/api/v1/auth/2fa/disable", json={"password": PASSWORD, "code": "000000"})
    assert r.status_code == 403
    assert admin_client.get("/api/v1/auth/2fa").json()["enabled"] is True

    # The failed attempts above did not spend the code.
    r = admin_client.post("/api/v1/auth/2fa/disable", json={"password": PASSWORD, "code": good})
    assert r.status_code == 204

    assert admin_client.get("/api/v1/auth/2fa").json()["enabled"] is False
    web = _fresh()
    assert _web_login(web).json()["access_token"]  # password alone again
    with get_session_factory()() as db:
        assert db.execute(text("SELECT count(*) FROM recovery_codes")).scalar_one() == 0
        assert (
            db.execute(text("SELECT totp_secret_enc FROM users WHERE username='admin'")).scalar_one() is None
        )


# --------------------------------------------------------------------------
# RustDesk client sign-in
# --------------------------------------------------------------------------


def test_the_client_is_asked_for_a_code(admin_client):
    _enable(admin_client)
    r = _client_login(_fresh())
    body = r.json()
    assert (body["type"], body["tfa_type"]) == ("email_check", "tfa_check")
    assert body["secret"] and body["user"]["name"] == "admin"
    assert "access_token" not in body


def test_the_client_completes_with_the_code_and_no_password(admin_client):
    secret, _ = _enable(admin_client)
    api = _fresh()
    first = _client_login(api).json()
    r = api.post(
        "/api/login",
        json={
            "type": "email_code",
            "username": "admin",
            "id": "1001",
            "uuid": "u",
            "secret": first["secret"],
            "verificationCode": totp.code_at(secret, NOW + 30),
            "tfaCode": totp.code_at(secret, NOW + 30),
            "autoLogin": True,
        },
    )
    body = r.json()
    assert body["type"] == "access_token" and body["user"]["name"] == "admin"
    me = api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert any(a["action"] == "login" and (a["detail"] or {}).get("two_factor") for a in _audit(admin_client))


def test_the_client_second_step_fails_on_anything_wrong(admin_client, app, monkeypatch):
    secret, codes = _enable(admin_client)
    _user(app, admin_client, "alice")
    api = _fresh()
    secret_token = _client_login(api).json()["secret"]
    good = totp.code_at(secret, NOW + 30)

    def second(**body):
        return api.post("/api/login", json={"type": "email_code", "username": "admin", **body}).json()

    assert "error" in second(secret=secret_token, tfaCode="000000")
    assert "error" in second(secret=secret_token, tfaCode=good, username="alice")  # someone else's challenge
    assert "error" in second(secret="not-a-secret", tfaCode=good)
    assert "error" in second(secret=secret_token, tfaCode=codes[0])  # the client cannot type recovery codes
    assert "error" in second(secret=secret_token)
    assert second(secret=secret_token, tfaCode=good).get("type") == "access_token"
    _clock(monkeypatch, NOW + 60)  # a fresh, valid code - but the challenge is spent
    assert "error" in second(secret=secret_token, tfaCode=totp.code_at(secret, NOW + 60))


def test_a_wrong_password_never_reaches_the_second_step(admin_client):
    _enable(admin_client)
    body = _fresh().post("/api/login", json={"username": "admin", "password": "nope-nope"}).json()
    assert "error" in body and "secret" not in body


def test_accounts_without_2fa_sign_in_as_before(admin_client, app):
    _enable(admin_client)
    _user(app, admin_client, "alice")
    body = _fresh().post("/api/login", json={"username": "alice", "password": "userpassword1"}).json()
    assert body["type"] == "access_token"
    web = _fresh()
    assert _web_login(web, "alice", "userpassword1").json()["access_token"]


# --------------------------------------------------------------------------
# Lost key, lost authenticator, key rotation
# --------------------------------------------------------------------------


def test_losing_the_key_fails_closed_instead_of_skipping_the_second_factor(admin_client, monkeypatch):
    _enable(admin_client)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", "")
    clear_settings_cache()
    web = _fresh()
    r = _web_login(web)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "TWO_FACTOR_UNAVAILABLE")
    assert "rd_session" not in web.cookies
    body = _client_login(_fresh()).json()
    assert "error" in body and "access_token" not in body


def test_a_different_key_cannot_open_the_secret_so_codes_fail(admin_client, monkeypatch):
    secret, codes = _enable(admin_client)
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", generate_key())
    clear_settings_cache()
    web = _fresh()
    challenge = _web_login(web).json()["challenge"]
    r = web.post(
        "/api/v1/auth/login/2fa", json={"challenge": challenge, "code": totp.code_at(secret, NOW + 30)}
    )
    assert r.status_code == 401
    # A recovery code does not depend on the key, so it still gets the user in.
    r = web.post(
        "/api/v1/auth/login/2fa", json={"challenge": _web_login(web).json()["challenge"], "code": codes[0]}
    )
    assert r.status_code == 200


def test_rotating_the_key_keeps_codes_working(admin_client, monkeypatch, _environment):
    secret, _ = _enable(admin_client)
    new_key = generate_key()
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", f"{new_key},{_environment}")
    clear_settings_cache()
    result = CliRunner().invoke(main, ["rotate-data-key"])
    assert result.exit_code == 0, result.output
    assert "1 two-factor secret" in result.output

    monkeypatch.setenv("DATA_ENCRYPTION_KEY", new_key)  # the old key is dropped
    clear_settings_cache()
    web = _fresh()
    r = web.post(
        "/api/v1/auth/login/2fa",
        json={"challenge": _web_login(web).json()["challenge"], "code": totp.code_at(secret, NOW + 30)},
    )
    assert r.status_code == 200


def test_an_administrator_can_reset_a_users_2fa(app, admin_client):
    alice, alice_id = _user(app, admin_client, "alice")
    _enable(alice)
    assert _web_login(_fresh(), "alice", "userpassword1").json()["two_factor_required"] is True

    assert alice.delete(f"/api/v1/users/{alice_id}/two-factor").status_code == 403
    assert admin_client.delete("/api/v1/users/9999/two-factor").status_code == 404

    r = admin_client.delete(f"/api/v1/users/{alice_id}/two-factor")
    assert r.status_code == 204
    assert alice.get("/api/v1/auth/me").status_code == 401  # signed out everywhere
    assert _web_login(_fresh(), "alice", "userpassword1").json()["access_token"]
    assert any(a["action"] == "two_factor_reset" for a in _audit(admin_client))
    assert admin_client.delete(f"/api/v1/users/{alice_id}/two-factor").status_code == 409


def test_the_cli_can_turn_2fa_off(admin_client, app):
    _, alice_id = _user(app, admin_client, "alice")
    alice = TestClient(app)
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "userpassword1"})
    alice.headers.update({"X-CSRF-Token": alice.cookies.get("rd_csrf")})
    _enable(alice)

    result = CliRunner().invoke(main, ["disable-2fa", "--username", "alice"])
    assert result.exit_code == 0 and "turned off" in result.output
    assert _web_login(_fresh(), "alice", "userpassword1").json()["access_token"]
    result = CliRunner().invoke(main, ["disable-2fa", "--username", "alice"])
    assert "is not on" in result.output
    assert CliRunner().invoke(main, ["disable-2fa", "--username", "ghost"]).exit_code != 0


def test_nothing_secret_reaches_the_audit_log_or_the_user_list(admin_client):
    secret, codes = _enable(admin_client)
    web = _fresh()
    web.post(
        "/api/v1/auth/login/2fa",
        json={"challenge": _web_login(web).json()["challenge"], "code": totp.code_at(secret, NOW + 30)},
    )
    dump = json.dumps(_audit(admin_client)) + json.dumps(admin_client.get("/api/v1/users").json())
    assert secret not in dump and not any(code in dump for code in codes)
    users = admin_client.get("/api/v1/users").json()
    assert users[0]["two_factor_enabled"] is True and "totp" not in json.dumps(users)
    actions = [a["action"] for a in _audit(admin_client)]
    assert "two_factor_enabled" in actions


def test_setup_returns_a_scannable_svg_qr_code(admin_client):
    """A QR code of the otpauth URI, so an authenticator app can scan it."""
    from urllib.parse import unquote

    body = admin_client.post("/api/v1/auth/2fa/setup").json()
    assert body["qr_svg"].startswith("data:image/svg+xml")
    svg = unquote(body["qr_svg"])
    assert "<svg" in svg and "<path" in svg
    # Only a picture: the secret must not appear in it as text.
    assert body["secret"] not in svg
