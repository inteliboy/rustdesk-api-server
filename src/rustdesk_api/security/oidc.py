"""OpenID Connect relying-party protocol: discovery, the authorization URL, the
code-for-token exchange and ID-token validation. No database access here; the
flow bookkeeping is in services/oidc.py.

What is checked, and why (an ID token is the whole proof of who signed in):

* Only the authorization-code flow with PKCE (S256) and a `state` and `nonce`.
* The token's signature against the provider's published keys, with an allow-list
  of *asymmetric* algorithms. `none` and the HMAC family are never accepted, so a
  token cannot be forged by "signing" it with the public key.
* `iss` equal to the discovered issuer, `aud` containing our client id (and `azp`
  equal to it when there are several audiences), `exp`/`iat` present and valid, the
  `nonce` this request was started with, and `sub` present.
* Every URL used (issuer, and each endpoint it names) is HTTPS. Plain HTTP is only
  allowed for a loopback host, which is how a development provider is reached.
  Redirects are not followed and answers are size-capped.

Only claims inside the ID token are used; the user-info endpoint is never called,
so the provider must put `email` there (scope `email`), which the common ones do.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

ALLOWED_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
MAX_RESPONSE_BYTES = 1_000_000
LEEWAY_SECONDS = 60
DISCOVERY_TTL_SECONDS = 3600
# An unknown signing key id triggers one refetch of the key set, but not more often than this.
JWKS_REFETCH_SECONDS = 300


class OidcError(Exception):
    """A sign-in that cannot go on. `public` is safe to show the user; `reason` is a
    short code for the audit log. Neither ever contains a token or a secret."""

    def __init__(self, public: str, reason: str) -> None:
        super().__init__(reason)
        self.public = public
        self.reason = reason


@dataclass(frozen=True)
class Provider:
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str
    redirect_uri: str
    auto_create_users: bool = False
    link_by_email: bool = False
    allowed_email_domains: frozenset[str] = frozenset()
    username_claim: str = "preferred_username"
    timeout_seconds: float = 10.0

    def __repr__(self) -> str:  # never show the secret, even in a traceback
        return f"Provider(name={self.name!r}, issuer={self.issuer!r})"


@dataclass
class _Cache:
    discovery: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    jwks: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_cache = _Cache()
# Tests point this at an in-process provider.
_transport: httpx.BaseTransport | None = None


def reset_caches() -> None:
    with _cache.lock:
        _cache.discovery.clear()
        _cache.jwks.clear()


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport
    reset_caches()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_https(url: str, what: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and _is_loopback(parts.hostname):
        return
    raise OidcError("The identity provider is not set up correctly.", f"{what}_not_https")


def _client(provider: Provider) -> httpx.Client:
    return httpx.Client(
        timeout=provider.timeout_seconds,
        follow_redirects=False,
        transport=_transport,
        headers={"User-Agent": "rustdesk-api-server", "Accept": "application/json"},
    )


def _json_request(client: httpx.Client, method: str, url: str, what: str, **kwargs: Any) -> dict[str, Any]:
    try:
        with client.stream(method, url, **kwargs) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise OidcError("The identity provider answered badly.", f"{what}_too_large")
            status = response.status_code
    except httpx.HTTPError as exc:
        raise OidcError("The identity provider could not be reached.", f"{what}_unreachable") from exc
    if status != 200:
        raise OidcError("The identity provider refused the request.", f"{what}_status_{status}")
    try:
        data = _loads(bytes(body))
    except ValueError as exc:
        raise OidcError("The identity provider answered badly.", f"{what}_not_json") from exc
    if not isinstance(data, dict):
        raise OidcError("The identity provider answered badly.", f"{what}_not_object")
    return data


def _loads(raw: bytes) -> Any:
    import json

    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Discovery and keys
# ---------------------------------------------------------------------------


def discover(provider: Provider) -> dict[str, Any]:
    now = time.monotonic()
    with _cache.lock:
        cached = _cache.discovery.get(provider.issuer)
        if cached and now - cached[0] < DISCOVERY_TTL_SECONDS:
            return cached[1]
    require_https(provider.issuer, "issuer")
    url = provider.issuer.rstrip("/") + "/.well-known/openid-configuration"
    with _client(provider) as client:
        doc = _json_request(client, "GET", url, "discovery")
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or issuer.rstrip("/") != provider.issuer.rstrip("/"):
        raise OidcError("The identity provider is not set up correctly.", "issuer_mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = doc.get(key)
        if not isinstance(value, str):
            raise OidcError("The identity provider is not set up correctly.", f"discovery_missing_{key}")
        require_https(value, key)
    with _cache.lock:
        _cache.discovery[provider.issuer] = (now, doc)
    return doc


def _fetch_jwks(provider: Provider, discovery: dict[str, Any]) -> dict[str, Any]:
    with _client(provider) as client:
        keys = _json_request(client, "GET", discovery["jwks_uri"], "jwks")
    if not isinstance(keys.get("keys"), list):
        raise OidcError("The identity provider answered badly.", "jwks_malformed")
    with _cache.lock:
        _cache.jwks[provider.issuer] = (time.monotonic(), keys)
    return keys


def _signing_key(provider: Provider, discovery: dict[str, Any], header: dict[str, Any]) -> Any:
    kid = header.get("kid")

    def find(key_set: dict[str, Any]) -> Any:
        candidates = []
        for entry in key_set["keys"]:
            if not isinstance(entry, dict) or entry.get("use") not in (None, "sig"):
                continue
            if kid is not None and entry.get("kid") != kid:
                continue
            try:
                candidates.append(jwt.PyJWK(entry))
            except (jwt.PyJWKError, ValueError, KeyError):
                continue  # a key type we do not handle; another entry may match
        # Without a `kid`, only an unambiguous key set can be used.
        if kid is None and len(candidates) != 1:
            return None
        return candidates[0].key if candidates else None

    with _cache.lock:
        cached = _cache.jwks.get(provider.issuer)
    key_set = cached[1] if cached else _fetch_jwks(provider, discovery)
    found = find(key_set)
    if found is None and cached and time.monotonic() - cached[0] > JWKS_REFETCH_SECONDS:
        # The provider may have rotated its keys.
        found = find(_fetch_jwks(provider, discovery))
    if found is None:
        raise OidcError("The sign-in could not be verified.", "signing_key_not_found")
    return found


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def derive(secret_key: str, purpose: str, salt: str) -> str:
    """A 256-bit value only this server can recompute from a request's stored salt.
    The PKCE verifier and the nonce are derived, not stored, so a copy of the
    database alone reveals neither."""
    mac = hmac.new(secret_key.encode("utf-8"), f"oidc:{purpose}:{salt}".encode(), hashlib.sha256)
    return _b64url(mac.digest())


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorization_url(
    provider: Provider, discovery: dict[str, Any], *, state: str, salt: str, secret_key: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri,
            "scope": provider.scopes,
            "state": state,
            "nonce": derive(secret_key, "nonce", salt),
            "code_challenge": pkce_challenge(derive(secret_key, "pkce", salt)),
            "code_challenge_method": "S256",
        }
    )
    endpoint = discovery["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"


def exchange_code(
    provider: Provider, discovery: dict[str, Any], *, code: str, salt: str, secret_key: str
) -> str:
    """Trades the authorization code for tokens and returns the ID token."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": provider.redirect_uri,
        "code_verifier": derive(secret_key, "pkce", salt),
    }
    auth: httpx.BasicAuth | None = None
    methods = discovery.get("token_endpoint_auth_methods_supported")
    if not provider.client_secret:
        form["client_id"] = provider.client_id  # a public client: PKCE is its only proof
    elif (
        isinstance(methods, list) and "client_secret_basic" not in methods and "client_secret_post" in methods
    ):
        form["client_id"] = provider.client_id
        form["client_secret"] = provider.client_secret
    else:
        auth = httpx.BasicAuth(provider.client_id, provider.client_secret)
    with _client(provider) as client:
        tokens = _json_request(client, "POST", discovery["token_endpoint"], "token", data=form, auth=auth)
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise OidcError("The identity provider did not return an identity.", "no_id_token")
    return id_token


def validate_id_token(
    provider: Provider, discovery: dict[str, Any], id_token: str, *, salt: str, secret_key: str
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OidcError("The sign-in could not be verified.", "token_malformed") from exc
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise OidcError("The sign-in could not be verified.", "token_algorithm")
    advertised = discovery.get("id_token_signing_alg_values_supported")
    if isinstance(advertised, list) and algorithm not in advertised:
        raise OidcError("The sign-in could not be verified.", "token_algorithm")

    key = _signing_key(provider, discovery, header)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[algorithm],
            audience=provider.client_id,
            issuer=discovery["issuer"],
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise OidcError("The sign-in expired. Try again.", "token_expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise OidcError("The sign-in could not be verified.", "token_audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise OidcError("The sign-in could not be verified.", "token_issuer") from exc
    except jwt.PyJWTError as exc:
        raise OidcError("The sign-in could not be verified.", "token_invalid") from exc

    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != provider.client_id:
        raise OidcError("The sign-in could not be verified.", "token_azp")
    expected_nonce = derive(secret_key, "nonce", salt)
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
        raise OidcError("The sign-in could not be verified.", "token_nonce")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise OidcError("The sign-in could not be verified.", "token_subject")
    return claims


def email_verified(claims: dict[str, Any]) -> bool:
    """`email_verified` is a boolean, but some providers send the string."""
    value = claims.get("email_verified")
    return value is True or (isinstance(value, str) and value.lower() == "true")
