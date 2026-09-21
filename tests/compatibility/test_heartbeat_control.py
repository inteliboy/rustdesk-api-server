"""What the API server can tell a client in its heartbeat response.

`hbbs_http/sync.rs` in the RustDesk client (1.4.9 and `master`) sends
`conns` (its live incoming connection ids) and `modified_at` (the strategy
version it last stored), and acts on `disconnect`, `strategy` and
`modified_at` in the response. These tests drive the real HTTP endpoints the
way that client does.
"""

from __future__ import annotations

UUID = "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2"


def _register(client, rustdesk_id="1001", uuid=UUID):
    r = client.post(
        "/api/sysinfo", json={"id": rustdesk_id, "uuid": uuid, "hostname": "h", "os": "linux / x"}
    )
    assert r.status_code == 200
    return next(d for d in client.get("/api/v1/devices").json()["items"] if d["rustdesk_id"] == rustdesk_id)


def _beat(client, rustdesk_id="1001", uuid=UUID, **extra):
    r = client.post("/api/heartbeat", json={"id": rustdesk_id, "uuid": uuid, **extra})
    assert r.status_code == 200
    return r.json()


def _device(client, device_id):
    return client.get(f"/api/v1/devices/{device_id}").json()


def _user_client(app, admin_client, name):
    """A separate logged-in client for an ordinary user."""
    from fastapi.testclient import TestClient

    r = admin_client.post("/api/v1/users", json={"username": name, "password": "userpassword1"})
    assert r.status_code == 201, r.text
    other = TestClient(app)
    other.post("/api/v1/auth/login", json={"username": name, "password": "userpassword1"})
    other.headers.update({"X-CSRF-Token": other.cookies.get("rd_csrf")})
    return other, r.json()["id"]


# --------------------------------------------------------------------------
# Live connections and disconnect
# --------------------------------------------------------------------------


def test_the_heartbeat_records_the_live_connections(admin_client):
    device = _register(admin_client)
    assert device["connection_count"] == 0

    _beat(admin_client, conns=[3, 1])
    assert _device(admin_client, device["id"])["connection_count"] == 2

    # The client omits `conns` when it has none.
    _beat(admin_client)
    assert _device(admin_client, device["id"])["connection_count"] == 0


def test_junk_in_conns_is_ignored(admin_client):
    device = _register(admin_client)
    _beat(admin_client, conns=[7, "8", -1, 0, True, None, 7, 2.5, 2**40])
    r = admin_client.get(f"/api/v1/devices/{device['id']}/connections")
    assert [c["id"] for c in r.json()] == [7]

    for junk in ("x", {"a": 1}, 5):
        _beat(admin_client, conns=junk)
    assert admin_client.get(f"/api/v1/devices/{device['id']}/connections").json() == []


def test_disconnect_reaches_the_client_once(admin_client):
    device = _register(admin_client)
    _beat(admin_client, conns=[4, 9])

    r = admin_client.post(f"/api/v1/devices/{device['id']}/disconnect", json={"connection_ids": [9]})
    assert r.status_code == 200, r.text
    assert [c["disconnect_requested"] for c in r.json()] == [False, True]

    first = _beat(admin_client, conns=[4, 9])
    assert first["disconnect"] == [9]
    assert "disconnect" not in _beat(admin_client, conns=[4, 9])


def test_disconnect_without_ids_ends_every_connection(admin_client):
    device = _register(admin_client)
    _beat(admin_client, conns=[2, 5])
    assert admin_client.post(f"/api/v1/devices/{device['id']}/disconnect", json={}).status_code == 200
    assert _beat(admin_client, conns=[2, 5])["disconnect"] == [2, 5]


def test_a_connection_that_ended_is_not_disconnected_later(admin_client):
    """Ids are per client process and get reused: a stale request must not end
    whatever holds that id afterwards."""
    device = _register(admin_client)
    _beat(admin_client, conns=[6])
    admin_client.post(f"/api/v1/devices/{device['id']}/disconnect", json={"connection_ids": [6]})

    response = _beat(admin_client)  # it closed by itself first
    assert "disconnect" not in response
    assert "disconnect" not in _beat(admin_client, conns=[6])


def test_disconnecting_an_unknown_connection_is_an_error(admin_client):
    device = _register(admin_client)
    _beat(admin_client, conns=[1])
    r = admin_client.post(f"/api/v1/devices/{device['id']}/disconnect", json={"connection_ids": [2]})
    assert r.status_code == 422
    assert "disconnect" not in _beat(admin_client, conns=[1])


def test_a_heartbeat_from_another_install_gets_no_control(admin_client):
    """The heartbeat is unauthenticated; the client's own uuid is what ties it
    to the device, so someone who only knows the id gets nothing and changes
    nothing."""
    device = _register(admin_client)
    _beat(admin_client, conns=[4])
    admin_client.post(f"/api/v1/devices/{device['id']}/disconnect", json={})

    intruder = _beat(admin_client, uuid="another-install", conns=[99, 100])
    assert intruder == {"data": "OK"}
    assert [c["id"] for c in admin_client.get(f"/api/v1/devices/{device['id']}/connections").json()] == [4]

    assert _beat(admin_client, conns=[4])["disconnect"] == [4]  # the real client still gets it


def test_only_the_owner_or_an_admin_can_disconnect(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    bob, _ = _user_client(app, admin_client, "bob")
    device = _register(admin_client)
    _beat(admin_client, conns=[1])

    # Not visible to alice at all.
    assert alice.post(f"/api/v1/devices/{device['id']}/disconnect", json={}).status_code == 404

    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    assert alice.post(f"/api/v1/devices/{device['id']}/disconnect", json={}).status_code == 200
    assert bob.post(f"/api/v1/devices/{device['id']}/disconnect", json={}).status_code == 404

    # A "control" share lets bob edit the record, not cut people off.
    admin_client.post(
        f"/api/v1/devices/{device['id']}/shares", json={"username": "bob", "permission": "control"}
    )
    assert bob.get(f"/api/v1/devices/{device['id']}/connections").status_code == 200
    assert bob.post(f"/api/v1/devices/{device['id']}/disconnect", json={}).status_code == 404


def test_connections_are_not_visible_to_those_who_cannot_see_the_device(app, admin_client):
    alice, _ = _user_client(app, admin_client, "alice")
    device = _register(admin_client)
    assert alice.get(f"/api/v1/devices/{device['id']}/connections").status_code == 404


def test_connections_show_who_is_connecting_from_the_connection_log(admin_client):
    device = _register(admin_client)
    _beat(admin_client, conns=[3])
    base = {"id": "1001", "uuid": UUID, "conn_id": 3, "session_id": 123456789}
    r = admin_client.post("/api/audit/conn", json={**base, "nonce": "n1", "action": "new", "ip": "10.0.0.5"})
    assert r.status_code == 200
    r = admin_client.post(
        "/api/audit/conn", json={**base, "nonce": "n2", "peer": ["2002", "Bob's laptop"], "type": 0}
    )
    assert r.status_code == 200
    (connection,) = admin_client.get(f"/api/v1/devices/{device['id']}/connections").json()
    assert (connection["id"], connection["peer_id"], connection["peer_name"]) == (3, "2002", "Bob's laptop")
    assert connection["from_ip"] == "10.0.0.5"


def test_disconnecting_is_audited(admin_client):
    device = _register(admin_client)
    _beat(admin_client, conns=[8])
    admin_client.post(f"/api/v1/devices/{device['id']}/disconnect", json={})
    items = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    entry = next(i for i in items if i["action"] == "connection_disconnect_requested")
    assert entry["detail"]["connection_ids"] == [8]


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------


def _strategy(admin_client, name="Locked down", options=None):
    options = {"enable-file-transfer": "N", "enable-clipboard": "N"} if options is None else options
    r = admin_client.post("/api/v1/strategies", json={"name": name, "options": options})
    assert r.status_code == 201, r.text
    return r.json()


def _assign(admin_client, device, strategy_id):
    r = admin_client.put(f"/api/v1/devices/{device['id']}/strategy", json={"strategy_id": strategy_id})
    assert r.status_code == 200, r.text


def test_a_device_without_a_strategy_gets_nothing(admin_client):
    _register(admin_client)
    assert _beat(admin_client, modified_at=0) == {"data": "OK"}
    assert _beat(admin_client) == {"data": "OK"}


def test_an_assigned_strategy_is_pushed_once_and_carries_every_catalog_key(admin_client):
    from rustdesk_api.services.strategies import CATALOG

    device = _register(admin_client)
    strategy = _strategy(admin_client)
    _assign(admin_client, device, strategy["id"])

    pushed = _beat(admin_client, modified_at=0)
    assert pushed["modified_at"] == strategy["modified_at"] > 0
    options = pushed["strategy"]["config_options"]
    assert set(options) == set(CATALOG)
    assert options["enable-file-transfer"] == "N" and options["enable-clipboard"] == "N"
    assert options["enable-audio"] == ""  # unset -> the client resets it to its default

    # The client stored `modified_at`, so it is not sent again.
    assert _beat(admin_client, modified_at=pushed["modified_at"]) == {"data": "OK"}


def test_editing_a_strategy_pushes_the_new_version(admin_client):
    device = _register(admin_client)
    strategy = _strategy(admin_client)
    _assign(admin_client, device, strategy["id"])
    have = _beat(admin_client, modified_at=0)["modified_at"]

    r = admin_client.put(
        f"/api/v1/strategies/{strategy['id']}",
        json={"name": "Locked down", "options": {"enable-file-transfer": "Y"}},
    )
    assert r.status_code == 200
    pushed = _beat(admin_client, modified_at=have)
    assert pushed["modified_at"] > have
    options = pushed["strategy"]["config_options"]
    assert options["enable-file-transfer"] == "Y"
    assert options["enable-clipboard"] == ""  # removed from the strategy -> reset

    # Renaming alone changes nothing the client cares about.
    admin_client.put(
        f"/api/v1/strategies/{strategy['id']}",
        json={"name": "Renamed", "options": {"enable-file-transfer": "Y"}},
    )
    assert _beat(admin_client, modified_at=pushed["modified_at"]) == {"data": "OK"}


def test_unassigning_resets_what_was_pushed_and_then_stays_quiet(admin_client):
    from rustdesk_api.services.strategies import CATALOG

    device = _register(admin_client)
    strategy = _strategy(admin_client)
    _assign(admin_client, device, strategy["id"])
    have = _beat(admin_client, modified_at=0)["modified_at"]

    _assign(admin_client, device, None)
    reset = _beat(admin_client, modified_at=have)
    assert reset["modified_at"] == 0
    assert reset["strategy"]["config_options"] == {key: "" for key in CATALOG}
    assert _beat(admin_client, modified_at=0) == {"data": "OK"}


def test_deleting_a_strategy_resets_its_devices(admin_client):
    device = _register(admin_client)
    strategy = _strategy(admin_client)
    _assign(admin_client, device, strategy["id"])
    have = _beat(admin_client, modified_at=0)["modified_at"]

    assert admin_client.delete(f"/api/v1/strategies/{strategy['id']}").status_code == 204
    assert _device(admin_client, device["id"])["strategy_id"] is None
    assert _beat(admin_client, modified_at=have)["modified_at"] == 0


def test_a_group_strategy_applies_unless_the_device_has_its_own(admin_client):
    device = _register(admin_client)
    group = admin_client.post("/api/v1/groups", json={"name": "Servers"}).json()
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"group_id": group["id"]})
    group_strategy = _strategy(admin_client, "For servers", {"enable-audio": "N"})
    own_strategy = _strategy(admin_client, "Own", {"enable-camera": "N"})

    r = admin_client.put(f"/api/v1/groups/{group['id']}/strategy", json={"strategy_id": group_strategy["id"]})
    assert r.status_code == 200
    body = _device(admin_client, device["id"])
    assert (body["strategy_id"], body["effective_strategy_name"]) == (None, "For servers")
    assert _beat(admin_client)["strategy"]["config_options"]["enable-audio"] == "N"

    _assign(admin_client, device, own_strategy["id"])
    body = _device(admin_client, device["id"])
    assert (body["strategy_name"], body["effective_strategy_name"]) == ("Own", "Own")
    options = _beat(admin_client, modified_at=group_strategy["modified_at"])["strategy"]["config_options"]
    assert options["enable-camera"] == "N" and options["enable-audio"] == ""


def test_a_heartbeat_with_odd_modified_at_still_works(admin_client):
    device = _register(admin_client)
    _assign(admin_client, device, _strategy(admin_client)["id"])
    for junk in ("abc", None, 1.5, [1], True):
        assert "strategy" in _beat(admin_client, modified_at=junk)


# --------------------------------------------------------------------------
# Strategy management API
# --------------------------------------------------------------------------


def test_strategies_are_administrator_only(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    device = _register(admin_client)
    strategy = _strategy(admin_client)

    for response in (
        alice.get("/api/v1/strategies"),
        alice.get("/api/v1/strategies/options"),
        alice.get(f"/api/v1/strategies/{strategy['id']}"),
        alice.post("/api/v1/strategies", json={"name": "x", "options": {}}),
        alice.put(f"/api/v1/strategies/{strategy['id']}", json={"name": "x", "options": {}}),
        alice.delete(f"/api/v1/strategies/{strategy['id']}"),
        alice.put(f"/api/v1/devices/{device['id']}/strategy", json={"strategy_id": strategy["id"]}),
    ):
        assert response.status_code == 403, response.request.url

    # Even for a device the user owns.
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    r = alice.put(f"/api/v1/devices/{device['id']}/strategy", json={"strategy_id": strategy["id"]})
    assert r.status_code == 403


def test_strategies_need_authentication(client):
    assert client.get("/api/v1/strategies").status_code == 401


def test_only_catalog_options_with_valid_values_are_accepted(admin_client):
    def create(options):
        return admin_client.post("/api/v1/strategies", json={"name": "s", "options": options})

    for options in (
        {"custom-rendezvous-server": "evil.example"},  # not a strategy option
        {"api-server": "http://evil.example"},
        {"key": "x"},
        {"whitelist": "0.0.0.0"},
        {"enable-file-transfer": "yes"},
        {"approve-mode": "always"},
        {"auto-disconnect-timeout": "0"},
        {"auto-disconnect-timeout": "5000"},
        {"auto-disconnect-timeout": "ten"},
        {"enable-audio": ["Y"]},
    ):
        r = create(options)
        assert r.status_code == 422, (options, r.text)
        assert r.json()["error"]["code"] == "INVALID_STRATEGY"

    assert (
        create({"auto-disconnect-timeout": "15", "approve-mode": "click", "enable-audio": ""}).status_code
        == 201
    )


def test_strategy_names_are_unique_ignoring_case(admin_client):
    _strategy(admin_client, "Kiosk")
    r = admin_client.post("/api/v1/strategies", json={"name": "kiosk", "options": {}})
    assert r.status_code == 409
    other = _strategy(admin_client, "Other")
    r = admin_client.put(f"/api/v1/strategies/{other['id']}", json={"name": "KIOSK", "options": {}})
    assert r.status_code == 409


def test_the_option_catalog_is_listed(admin_client):
    options = admin_client.get("/api/v1/strategies/options").json()
    by_key = {o["key"]: o for o in options}
    assert by_key["enable-file-transfer"]["choices"] == ["Y", "N"]
    assert by_key["approve-mode"]["choices"] == ["password", "click"]
    assert by_key["auto-disconnect-timeout"]["kind"] == "int"
    assert not {"custom-rendezvous-server", "api-server", "key", "relay-server", "whitelist"} & set(by_key)


def test_strategy_usage_counts_and_assignment_errors(admin_client):
    device = _register(admin_client)
    group = admin_client.post("/api/v1/groups", json={"name": "G"}).json()
    strategy = _strategy(admin_client)
    _assign(admin_client, device, strategy["id"])
    admin_client.put(f"/api/v1/groups/{group['id']}/strategy", json={"strategy_id": strategy["id"]})

    (listed,) = admin_client.get("/api/v1/strategies").json()
    assert (listed["device_count"], listed["group_count"]) == (1, 1)

    assert admin_client.put("/api/v1/devices/9999/strategy", json={"strategy_id": None}).status_code == 404
    assert admin_client.put("/api/v1/groups/9999/strategy", json={"strategy_id": None}).status_code == 404
    r = admin_client.put(f"/api/v1/devices/{device['id']}/strategy", json={"strategy_id": 9999})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "STRATEGY_NOT_FOUND")


def test_strategy_changes_are_audited(admin_client):
    device = _register(admin_client)
    strategy = _strategy(admin_client)
    _assign(admin_client, device, strategy["id"])
    actions = [i["action"] for i in admin_client.get("/api/v1/admin/audit-logs").json()["items"]]
    assert "strategy_created" in actions and "strategy_assigned" in actions


def test_a_users_own_view_names_the_strategy_but_not_its_options(app, admin_client):
    alice, alice_id = _user_client(app, admin_client, "alice")
    device = _register(admin_client)
    admin_client.patch(f"/api/v1/devices/{device['id']}", json={"owner_id": alice_id})
    _assign(admin_client, device, _strategy(admin_client, "Corporate")["id"])

    body = alice.get(f"/api/v1/devices/{device['id']}").json()
    assert body["effective_strategy_name"] == "Corporate"
    assert "options" not in body and "enable-file-transfer" not in str(body)


def test_options_the_client_reads_from_its_config_can_be_set(admin_client):
    options = {
        "temporary-password-length": "10",
        "allow-numeric-one-time-password": "Y",
        "allow-scope-violation-close": "Y",
        "enable-remote-printer": "N",
        "enable-hwcodec": "N",
    }
    r = admin_client.post("/api/v1/strategies", json={"name": "Tight", "options": options})
    assert r.status_code == 201, r.text
    bad = {"temporary-password-length": "7"}
    assert admin_client.post("/api/v1/strategies", json={"name": "Bad", "options": bad}).status_code == 422


def test_options_a_strategy_cannot_reach_are_not_offered_and_old_ones_are_dropped(admin_client):
    from rustdesk_api.services.strategies import CATALOG, RETIRED_KEYS

    assert RETIRED_KEYS and not RETIRED_KEYS & set(CATALOG)
    offered = {item["key"] for item in admin_client.get("/api/v1/strategies/options").json()}
    assert not RETIRED_KEYS & offered

    # A strategy saved before they were retired still saves; the value is dropped.
    old = {"lock_after_session_end": "Y", "enable-audio": "N"}
    r = admin_client.post("/api/v1/strategies", json={"name": "Old", "options": old})
    assert r.status_code == 201, r.text
    assert r.json()["options"] == {"enable-audio": "N"}
