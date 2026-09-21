"""OpenID Connect sign-in, against an in-process identity provider.

The provider (`FakeIdp`) is real where it matters: RSA-signed ID tokens, a
discovery document, a key set, and a token endpoint that checks the PKCE
verifier, the redirect URI and the client's credentials. It is reached through
`httpx.MockTransport`, so nothing leaves the process.

The RustDesk client leg (`POST /api/oidc/auth`, `GET /api/oidc/auth-query`) is
taken from the client source (`hbbs_http/account.rs`) and NOT yet seen against a
real client or a real provider.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from rustdesk_api.app import create_app
from rustdesk_api.config import Settings, clear_settings_cache, get_settings
from rustdesk_api.db.database import get_session_factory, init_engine
from rustdesk_api.db.migrations.runner import run_migrations
from rustdesk_api.models.oidc import OidcIdentity, OidcRequest
from rustdesk_api.models.user import User
from rustdesk_api.security import oidc as oidc_security

ISSUER = "https://idp.example.test"
CLIENT_ID = "rd-client"
CLIENT_SECRET = "s3cret-value-for-tests"
EXTERNAL = "https://rd.example.test"
REDIRECT = f"{EXTERNAL}/api/oidc/callback"
PENDING = "No authed oidc is found"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeIdp:
    kid = "kid-1"

    def __init__(self) -> None:
        self.codes: dict[str, dict] = {}
        self.discovery_issuer = ISSUER
        self.token_status = 200

    def jwk(self) -> dict:
        data = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
        return {**data, "kid": self.kid, "use": "sig", "alg": "RS256"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.discovery_issuer,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if path == "/token":
            return self._token(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "server_error"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = self.codes.pop(form.get("code", ""), None)  # a code works once
        if grant is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != grant["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != grant["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200, json={"access_token": "at", "token_type": "Bearer", "id_token": grant["token"]}
        )

    def authorize(
        self,
        url: str,
        *,
        sub: str = "sub-1",
        email: str | None = "alice@corp.test",
        email_verified: bool = True,
        overrides: dict | None = None,
        drop: tuple[str, ...] = (),
        token: str | None = None,
    ) -> tuple[str, str]:
        """What the provider does after the user signs in: returns (code, state)."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/authorize"
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256"
        assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == REDIRECT
        assert "openid" in q["scope"].split()
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "nonce": q["nonce"],
            "email_verified": email_verified,
            "preferred_username": "alice.sso",
        }
        if email is not None:
            claims["email"] = email
        claims.update(overrides or {})
        for key in drop:
            claims.pop(key, None)
        if token is None:
            token = jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": self.kid})
        code = secrets.token_urlsafe(9)
        self.codes[code] = {
            "challenge": q["code_challenge"],
            "redirect_uri": q["redirect_uri"],
            "token": token,
        }
        return code, q["state"]


@pytest.fixture()
def idp():
    fake = FakeIdp()
    oidc_security.set_transport(httpx.MockTransport(fake.handler))
    yield fake
    oidc_security.set_transport(None)


def make_app(monkeypatch, settings: Settings, **extra: str):
    env = {
        "OIDC_ISSUER": ISSUER,
        "OIDC_CLIENT_ID": CLIENT_ID,
        "OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "SECRET_KEY": "k" * 32,
        "EXTERNAL_URL": EXTERNAL,
        **extra,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    current = get_settings()
    init_engine(current)
    run_migrations(current)
    return create_app(current)


@pytest.fixture()
def sso(monkeypatch, settings, idp):
    return make_app(monkeypatch, settings)


def _csrf(client: TestClient) -> TestClient:
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})
    return client


def _signed_in(app, username: str, password: str) -> TestClient:
    client = TestClient(app)
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return _csrf(client)


def _admin(app) -> TestClient:
    client = TestClient(app)
    client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "adminpass123", "email": "admin@corp.test"},
    )
    return _signed_in(app, "admin", "adminpass123")


def _make_user(admin: TestClient, username="alice", email="alice@corp.test", password="alicepassword1"):
    r = admin.post("/api/v1/users", json={"username": username, "password": password, "email": email})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _link(app, idp: FakeIdp, user_client: TestClient, **claims) -> None:
    """The user links their provider account from the Security page."""
    r = user_client.post("/api/v1/auth/oidc/link")
    assert r.status_code == 200, r.text
    code, state = idp.authorize(r.json()["url"], **claims)
    back = user_client.get(
        "/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False
    )
    assert back.status_code == 303 and back.headers["location"] == "/security?sso=linked", back.text


def _start(rd: TestClient, uid="1001", uuid="uuid-1001", op="sso") -> dict:
    return rd.post(
        "/api/oidc/auth", json={"op": op, "id": uid, "uuid": uuid, "deviceInfo": {}, "apiDomain": "x"}
    ).json()


def _poll(rd: TestClient, code: str, uid="1001", uuid="uuid-1001") -> dict:
    return rd.get("/api/oidc/auth-query", params={"code": code, "id": uid, "uuid": uuid}).json()


def _finish_in_browser(app, idp: FakeIdp, started: dict, **claims) -> httpx.Response:
    browser = TestClient(app)
    code, state = idp.authorize(started["url"], **claims)
    return browser.get("/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)


def _audit(admin: TestClient) -> list[dict]:
    return admin.get("/api/v1/admin/audit-logs", params={"page_size": 200}).json()["items"]


def _db():
    return get_session_factory()()


# ---------------------------------------------------------------------------
# Client flow
# ---------------------------------------------------------------------------


def test_login_options_offers_the_provider_only_when_configured(sso):
    assert TestClient(sso).get("/api/login-options").json() == ["oidc/sso"]


def test_nothing_is_offered_or_started_without_a_provider(client):
    assert client.get("/api/login-options").json() == []
    assert client.get("/api/v1/auth/options").json()["oidc_name"] is None
    assert (
        "not set up"
        in client.post("/api/oidc/auth", json={"op": "sso", "id": "1", "uuid": "u"}).json()["error"]
    )
    assert client.get("/api/oidc/callback", params={"code": "x", "state": "y"}).status_code == 404
    assert (
        client.get("/api/v1/auth/oidc/login", follow_redirects=False).headers["location"]
        == "/login?sso=unavailable"
    )


def test_the_client_signs_in_once_the_browser_finishes(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")

    rd = TestClient(sso)
    started = _start(rd)
    assert set(started) == {"code", "url"}
    assert _poll(rd, started["code"]) == {"error": PENDING}

    page = _finish_in_browser(sso, idp, started, sub="alice-sub")
    assert page.status_code == 200 and "close this window" in page.text
    assert page.headers["cache-control"] == "no-store"

    body = _poll(rd, started["code"])
    assert body["type"] == "access_token" and body["access_token"]
    assert body["user"]["name"] == "alice" and body["user"]["info"] == {}
    # The token is a real client session.
    current = rd.post(
        "/api/currentUser", json={}, headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert current.json()["name"] == "alice"
    # A result is handed over once.
    assert "error" in _poll(rd, started["code"]) and PENDING not in _poll(rd, started["code"])["error"]


def test_a_leaked_handle_is_useless_from_another_device(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    rd = TestClient(sso)
    started = _start(rd)
    _finish_in_browser(sso, idp, started, sub="alice-sub")

    thief = TestClient(sso)
    assert "access_token" not in _poll(thief, started["code"], uid="9999", uuid="other")
    assert "access_token" not in _poll(thief, started["code"], uuid="other")
    assert "access_token" in _poll(rd, started["code"])  # the real device still gets it


def test_start_refuses_an_unknown_provider_or_a_missing_device(sso):
    rd = TestClient(sso)
    assert "error" in _start(rd, op="google")
    assert "error" in rd.post("/api/oidc/auth", json={"op": "sso"}).json()


def test_the_authorization_request_carries_state_pkce_and_nonce_and_stores_no_secret(sso, idp):
    started = _start(TestClient(sso))
    q = {k: v[0] for k, v in parse_qs(urlsplit(started["url"]).query).items()}
    assert len(q["state"]) >= 40 and len(q["nonce"]) >= 40 and len(q["code_challenge"]) >= 40
    with _db() as db:
        row = db.query(OidcRequest).one()
        stored = " ".join(str(getattr(row, c.name)) for c in OidcRequest.__table__.columns)
    for secret in (q["state"], q["nonce"], started["code"]):
        assert secret not in stored  # only hashes; the verifier and nonce are derived, not kept


# ---------------------------------------------------------------------------
# Who the provider account is
# ---------------------------------------------------------------------------


def test_an_unlinked_account_is_refused_and_nothing_is_created(sso, idp):
    admin = _admin(sso)
    _make_user(admin)  # alice@corp.test exists locally but is not linked
    rd = TestClient(sso)
    started = _start(rd)
    page = _finish_in_browser(sso, idp, started, sub="stranger", email="alice@corp.test")
    assert page.status_code == 400 and "No account is linked" in page.text
    assert "No account is linked" in _poll(rd, started["code"])["error"]
    with _db() as db:
        assert db.query(User).count() == 2 and db.query(OidcIdentity).count() == 0
    failure = [e for e in _audit(admin) if e["action"] == "login" and e["result"] == "failure"][0]
    assert failure["detail"]["reason"] == "not_linked" and failure["detail"]["via"] == "oidc_client"


def test_link_by_email_needs_the_option_and_a_verified_address(monkeypatch, settings, idp):
    app = make_app(monkeypatch, settings, OIDC_LINK_BY_EMAIL="true")
    admin = _admin(app)
    _make_user(admin)
    rd = TestClient(app)

    started = _start(rd)
    _finish_in_browser(app, idp, started, sub="s-unverified", email_verified=False)
    assert "No account is linked" in _poll(rd, started["code"])["error"]

    started = _start(rd)
    _finish_in_browser(app, idp, started, sub="s-1")
    assert _poll(rd, started["code"])["user"]["name"] == "alice"
    with _db() as db:
        assert db.query(OidcIdentity).one().subject == "s-1"

    # The identity, not the address, is what counts from now on.
    started = _start(rd)
    _finish_in_browser(app, idp, started, sub="s-1", email="someone.else@corp.test")
    assert _poll(rd, started["code"])["user"]["name"] == "alice"


def test_email_linking_is_off_by_default(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    rd = TestClient(sso)
    started = _start(rd)
    _finish_in_browser(sso, idp, started, sub="s-1")
    assert "access_token" not in _poll(rd, started["code"])


def test_auto_created_users_are_plain_users_who_cannot_use_a_password(monkeypatch, settings, idp):
    app = make_app(
        monkeypatch, settings, OIDC_AUTO_CREATE_USERS="true", OIDC_ALLOWED_EMAIL_DOMAINS="corp.test"
    )
    admin = _admin(app)
    rd = TestClient(app)
    started = _start(rd)
    _finish_in_browser(app, idp, started, sub="new-1", email="bob@corp.test")
    body = _poll(rd, started["code"])
    assert body["user"]["name"] == "alice.sso" and body["user"]["is_admin"] is False

    with _db() as db:
        user = db.query(User).filter(User.username == "alice.sso").one()
        assert user.is_admin is False and user.email == "bob@corp.test"
        assert user.password_hash.startswith("!")
    for password in ("", "!sso-only", "alicepassword1"):
        r = TestClient(app).post(
            "/api/v1/auth/login", json={"username": "alice.sso", "password": password or "x"}
        )
        assert r.status_code in (401, 422)
    assert any(e["action"] == "user_created" and e["detail"].get("via") == "oidc" for e in _audit(admin))

    # A domain that is not allowed, or an address a local account already uses.
    started = _start(rd)
    _finish_in_browser(app, idp, started, sub="new-2", email="eve@elsewhere.test")
    assert "No account is linked" in _poll(rd, started["code"])["error"]
    _make_user(admin, username="carol", email="carol@corp.test")
    started = _start(rd)
    _finish_in_browser(app, idp, started, sub="new-3", email="carol@corp.test")
    assert "access_token" not in _poll(rd, started["code"])


def test_auto_create_will_not_start_without_a_domain_list(monkeypatch, settings, idp):
    with pytest.raises(ValueError):
        make_app(monkeypatch, settings, OIDC_AUTO_CREATE_USERS="true")


def test_oidc_will_not_start_with_a_weak_secret_key_or_a_plain_http_issuer(monkeypatch, settings, idp):
    with pytest.raises(ValueError):
        make_app(monkeypatch, settings, SECRET_KEY="change-me")
    with pytest.raises(ValueError):
        make_app(monkeypatch, settings, OIDC_ISSUER="http://idp.example.test")
    with pytest.raises(ValueError):
        make_app(monkeypatch, settings, OIDC_CLIENT_ID="")


def test_a_disabled_account_cannot_sign_in(sso, idp):
    admin = _admin(sso)
    made = _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    assert admin.patch(f"/api/v1/users/{made['id']}", json={"is_active": False}).status_code == 200

    rd = TestClient(sso)
    started = _start(rd)
    _finish_in_browser(sso, idp, started, sub="alice-sub")
    assert "disabled" in _poll(rd, started["code"])["error"]


# ---------------------------------------------------------------------------
# The state, the browser and the token
# ---------------------------------------------------------------------------


def test_a_state_works_once_and_an_unknown_one_not_at_all(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    started = _start(TestClient(sso))
    code, state = idp.authorize(started["url"], sub="alice-sub")
    browser = TestClient(sso)
    assert browser.get("/api/oidc/callback", params={"code": code, "state": state}).status_code == 200
    again = browser.get("/api/oidc/callback", params={"code": code, "state": state})
    assert again.status_code == 400 and "no longer valid" in again.text
    assert browser.get("/api/oidc/callback", params={"code": "x", "state": "nope"}).status_code == 400
    assert browser.get("/api/oidc/callback").status_code == 400


def test_an_expired_request_is_refused(sso, idp):
    started = _start(TestClient(sso))
    with _db() as db:
        row = db.query(OidcRequest).one()
        row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        db.commit()
    page = _finish_in_browser(sso, idp, started, sub="alice-sub")
    assert page.status_code == 400 and "no longer valid" in page.text


def test_a_provider_error_ends_the_sign_in(sso, idp):
    admin = _admin(sso)
    rd = TestClient(sso)
    started = _start(rd)
    state = parse_qs(urlsplit(started["url"]).query)["state"][0]
    page = TestClient(sso).get("/api/oidc/callback", params={"error": "access_denied", "state": state})
    assert page.status_code == 400
    assert "did not allow" in _poll(rd, started["code"])["error"]
    assert any(e["detail"].get("reason") == "provider_access_denied" for e in _audit(admin))


@pytest.mark.parametrize(
    "why, kwargs",
    [
        ("wrong nonce", {"overrides": {"nonce": "not-the-nonce"}}),
        ("wrong audience", {"overrides": {"aud": "someone-else"}}),
        ("wrong issuer", {"overrides": {"iss": "https://evil.example.test"}}),
        ("expired", {"overrides": {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}}),
        ("no subject", {"drop": ("sub",)}),
        ("no expiry", {"drop": ("exp",)}),
        ("several audiences without azp", {"overrides": {"aud": [CLIENT_ID, "other"]}}),
    ],
)
def test_a_bad_id_token_is_refused(sso, idp, why, kwargs):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    rd = TestClient(sso)
    started = _start(rd)
    _finish_in_browser(sso, idp, started, sub="alice-sub", **kwargs)
    assert "access_token" not in _poll(rd, started["code"]), why


def _forged(idp: FakeIdp, started: dict, algorithm: str) -> str:
    q = {k: v[0] for k, v in parse_qs(urlsplit(started["url"]).query).items()}
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "alice-sub",
        "iat": now,
        "exp": now + 300,
        "nonce": q["nonce"],
    }
    if algorithm == "none":
        return jwt.encode(claims, None, algorithm="none", headers={"kid": idp.kid})
    if algorithm == "other-key":
        return jwt.encode(claims, _OTHER_KEY, algorithm="RS256", headers={"kid": idp.kid})
    # HS256 "signed" with the public key: the classic algorithm-confusion forgery.
    public_pem = _KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": idp.kid}).encode())
    body = _b64(json.dumps(claims).encode())
    signature = hmac.new(public_pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(signature)}"


@pytest.mark.parametrize("algorithm", ["none", "other-key", "hs256-with-public-key"])
def test_a_forged_id_token_is_refused(sso, idp, algorithm):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    rd = TestClient(sso)
    started = _start(rd)
    _finish_in_browser(sso, idp, started, token=_forged(idp, started, algorithm))
    assert "access_token" not in _poll(rd, started["code"])
    with _db() as db:
        assert db.query(User).count() == 2


def test_a_token_from_another_sign_in_is_refused(sso, idp):
    """A valid token for one request must not finish another (the nonce differs)."""
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    rd = TestClient(sso)
    first, second = _start(rd, uid="1001"), _start(rd, uid="1002", uuid="uuid-1002")
    code, _ = idp.authorize(first["url"], sub="alice-sub")
    _, state_2 = idp.authorize(second["url"], sub="alice-sub")
    TestClient(sso).get("/api/oidc/callback", params={"code": code, "state": state_2})
    assert "access_token" not in _poll(rd, second["code"], uid="1002", uuid="uuid-1002")


def test_provider_trouble_is_reported_not_raised(sso, idp):
    rd = TestClient(sso)
    started = _start(rd)
    idp.token_status = 500
    page = _finish_in_browser(sso, idp, started, sub="alice-sub")
    assert page.status_code == 400 and "s3cret" not in page.text
    assert "error" in _poll(rd, started["code"])

    idp.discovery_issuer = "https://evil.example.test"
    oidc_security.reset_caches()
    assert "error" in _start(rd)


# ---------------------------------------------------------------------------
# WebUI
# ---------------------------------------------------------------------------


def test_sign_in_to_the_webui_with_the_provider(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")

    browser = TestClient(sso)
    start = browser.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"].startswith(f"{ISSUER}/authorize?")
    assert "rd_oidc" in start.headers["set-cookie"] and "HttpOnly" in start.headers["set-cookie"]
    code, state = idp.authorize(start.headers["location"], sub="alice-sub")
    back = browser.get("/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/"
    assert browser.get("/api/v1/auth/me").json()["username"] == "alice"
    assert any(e["detail"].get("via") == "webui_oidc" for e in _audit(admin))


def test_a_webui_sign_in_cannot_be_finished_in_another_browser(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")

    victim = TestClient(sso)
    start = victim.get("/api/v1/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(start.headers["location"], sub="alice-sub")
    attacker = TestClient(sso)  # no cookie: the link was sent to somebody else
    page = attacker.get("/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert page.status_code == 400 and "different browser" in page.text
    assert attacker.get("/api/v1/auth/me").status_code == 401
    # The browser that started it can still finish.
    ok = victim.get("/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert ok.status_code == 303


def test_linking_needs_a_signed_in_user_and_csrf(sso):
    assert TestClient(sso).post("/api/v1/auth/oidc/link").status_code in (401, 403)
    admin = _admin(sso)
    _make_user(admin)
    plain = TestClient(sso)
    plain.post("/api/v1/auth/login", json={"username": "alice", "password": "alicepassword1"})
    assert plain.post("/api/v1/auth/oidc/link").status_code == 403  # no CSRF header


def test_a_provider_account_can_only_be_linked_to_one_user(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _make_user(admin, username="bob", email="bob@corp.test")
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="shared-sub")

    bob = _signed_in(sso, "bob", "alicepassword1")
    r = bob.post("/api/v1/auth/oidc/link")
    code, state = idp.authorize(r.json()["url"], sub="shared-sub")
    page = bob.get("/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert page.status_code == 400 and "already linked" in page.text


def test_unlinking_is_only_for_your_own_identity(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _make_user(admin, username="bob", email="bob@corp.test")
    alice = _signed_in(sso, "alice", "alicepassword1")
    _link(sso, idp, alice, sub="alice-sub")
    identity = alice.get("/api/v1/auth/oidc/identities").json()
    assert identity["enabled"] and len(identity["identities"]) == 1 and identity["has_password"]
    identity_id = identity["identities"][0]["id"]

    bob = _signed_in(sso, "bob", "alicepassword1")
    assert bob.delete(f"/api/v1/auth/oidc/identities/{identity_id}").status_code == 404  # not IDOR-able
    assert alice.delete(f"/api/v1/auth/oidc/identities/{identity_id}").status_code == 204
    assert alice.get("/api/v1/auth/oidc/identities").json()["identities"] == []
    assert any(e["action"] == "oidc_unlinked" for e in _audit(admin))


def test_the_last_way_in_cannot_be_unlinked(monkeypatch, settings, idp):
    app = make_app(
        monkeypatch, settings, OIDC_AUTO_CREATE_USERS="true", OIDC_ALLOWED_EMAIL_DOMAINS="corp.test"
    )
    _admin(app)
    browser = TestClient(app)
    start = browser.get("/api/v1/auth/oidc/login", follow_redirects=False)
    code, state = idp.authorize(start.headers["location"], sub="new-1", email="bob@corp.test")
    browser.get("/api/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
    _csrf(browser)
    listing = browser.get("/api/v1/auth/oidc/identities").json()
    assert listing["has_password"] is False
    r = browser.delete(f"/api/v1/auth/oidc/identities/{listing['identities'][0]['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "LAST_LOGIN_METHOD"


def test_the_sign_in_page_is_told_about_the_provider(sso):
    assert TestClient(sso).get("/api/v1/auth/options").json()["oidc_name"] == "sso"


def test_no_secret_reaches_the_audit_log_or_the_database(sso, idp):
    admin = _admin(sso)
    _make_user(admin)
    _link(sso, idp, _signed_in(sso, "alice", "alicepassword1"), sub="alice-sub")
    rd = TestClient(sso)
    started = _start(rd)
    code, state = idp.authorize(started["url"], sub="alice-sub")
    id_token = idp.codes[code]["token"]
    TestClient(sso).get("/api/oidc/callback", params={"code": code, "state": state})

    dump = json.dumps(_audit(admin))
    with _db() as db:
        dump += " ".join(str(r) for r in db.execute(OidcRequest.__table__.select()).all())
        dump += " ".join(str(r) for r in db.execute(OidcIdentity.__table__.select()).all())
    for secret in (CLIENT_SECRET, id_token, code, state):
        assert secret not in dump


def test_expired_requests_are_purged(sso):
    from rustdesk_api.services import oidc as oidc_service

    _start(TestClient(sso))
    with _db() as db:
        row = db.query(OidcRequest).one()
        row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
        db.commit()
        assert oidc_service.purge_expired(db) == 1
        db.commit()
        assert db.query(OidcRequest).count() == 0


def test_an_unexpected_failure_ends_the_sign_in_instead_of_hanging_it(sso, idp, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider sent something we cannot parse")

    monkeypatch.setattr(oidc_security, "exchange_code", boom)
    rd = TestClient(sso)
    started = _start(rd)
    page = _finish_in_browser(sso, idp, started, sub="alice-sub")
    assert page.status_code == 400 and "cannot parse" not in page.text
    assert _poll(rd, started["code"])["error"] == "The sign-in failed. Try again."
