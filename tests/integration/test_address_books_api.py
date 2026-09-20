"""WebUI/management API for address books (/api/v1/address-books and the
`?book=` selector on /api/v1/address-book): listing, shared-book CRUD, sharing,
and per-rule access to a shared book's entries."""

PASSWORD = "alicepassword1"


def _login_as(client, username, password):
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login", json={"username": username, "password": password})
    client.headers.update({"X-CSRF-Token": client.cookies.get("rd_csrf")})


def _make_user(admin_client, username):
    r = admin_client.post(
        "/api/v1/users", json={"username": username, "password": PASSWORD, "is_admin": False}
    )
    assert r.status_code == 201


def _book(client, name="Team", **extra):
    r = client.post("/api/v1/address-books", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _share(client, guid, username, rule):
    return client.put(f"/api/v1/address-books/{guid}/shares", json={"username": username, "rule": rule})


def test_list_starts_with_the_personal_book(admin_client):
    books = admin_client.get("/api/v1/address-books").json()
    assert len(books) == 1
    personal = books[0]
    assert (personal["name"], personal["is_personal"], personal["rule"], personal["is_owner"]) == (
        "My address book",
        True,
        3,
        True,
    )
    assert personal["can_write"] is True
    assert personal["entry_count"] == 0


def test_requires_authentication(client):
    assert client.get("/api/v1/address-books").status_code == 401
    assert client.post("/api/v1/address-books", json={"name": "x"}).status_code == 401


def test_create_shared_book(admin_client):
    book = _book(admin_client, "Servers", note="Prod boxes")
    assert (book["name"], book["note"], book["is_personal"], book["owner"]) == (
        "Servers",
        "Prod boxes",
        False,
        "admin",
    )
    names = [b["name"] for b in admin_client.get("/api/v1/address-books").json()]
    assert names == ["My address book", "Servers"]


def test_book_names_are_unique_per_owner_and_the_personal_name_is_reserved(admin_client):
    _book(admin_client, "Servers")
    assert admin_client.post("/api/v1/address-books", json={"name": "servers"}).status_code == 409
    assert admin_client.post("/api/v1/address-books", json={"name": "My Address Book"}).status_code == 409
    assert admin_client.post("/api/v1/address-books", json={"name": "   "}).status_code == 422


def test_mutations_need_the_csrf_token(admin_client):
    r = admin_client.post("/api/v1/address-books", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403


def test_rename_and_delete_a_shared_book(admin_client):
    guid = _book(admin_client, "Old")["guid"]
    admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "111"})

    r = admin_client.patch(f"/api/v1/address-books/{guid}", json={"name": "New", "note": "n"})
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["entry_count"]) == ("New", 1)

    assert admin_client.delete(f"/api/v1/address-books/{guid}").status_code == 204
    assert [b["name"] for b in admin_client.get("/api/v1/address-books").json()] == ["My address book"]
    assert admin_client.get(f"/api/v1/address-book?book={guid}").status_code == 404


def test_the_personal_book_cannot_be_renamed_deleted_or_shared(admin_client):
    _make_user(admin_client, "alice")
    guid = admin_client.get("/api/v1/address-books").json()[0]["guid"]
    assert admin_client.patch(f"/api/v1/address-books/{guid}", json={"name": "x"}).status_code == 422
    assert admin_client.delete(f"/api/v1/address-books/{guid}").status_code == 422
    assert _share(admin_client, guid, "alice", 1).status_code == 422


def test_sharing_validates_the_user_and_the_rule(admin_client):
    _make_user(admin_client, "alice")
    guid = _book(admin_client)["guid"]
    assert _share(admin_client, guid, "nobody", 1).status_code == 404
    assert _share(admin_client, guid, "admin", 1).status_code == 422  # the owner
    assert _share(admin_client, guid, "alice", 0).status_code == 422
    assert _share(admin_client, guid, "alice", 4).status_code == 422
    assert _share(admin_client, guid, "alice", 2).status_code == 200


def test_changing_a_rule_updates_the_share_instead_of_duplicating_it(admin_client):
    _make_user(admin_client, "alice")
    guid = _book(admin_client)["guid"]
    _share(admin_client, guid, "alice", 1)
    body = _share(admin_client, guid, "alice", 3).json()
    assert [(s["username"], s["rule"]) for s in body["shares"]] == [("alice", 3)]


def test_a_shared_user_sees_the_book_with_their_rule_but_not_the_share_list(admin_client):
    _make_user(admin_client, "alice")
    _make_user(admin_client, "bob")
    guid = _book(admin_client, "Team")["guid"]
    _share(admin_client, guid, "alice", 1)
    _share(admin_client, guid, "bob", 2)

    _login_as(admin_client, "alice", PASSWORD)
    team = next(b for b in admin_client.get("/api/v1/address-books").json() if b["guid"] == guid)
    assert (team["rule"], team["can_write"], team["is_owner"], team["owner"]) == (1, False, False, "admin")
    assert team["shares"] == []  # who else has access is the owner's business


def test_only_the_owner_can_change_or_share_a_book(admin_client):
    """A user who merely has access gets the same 404 as a stranger, so the
    book's existence is not confirmed to them either."""
    _make_user(admin_client, "alice")
    _make_user(admin_client, "mallory")
    guid = _book(admin_client, "Team")["guid"]
    _share(admin_client, guid, "alice", 3)  # even full control is not ownership

    for who in ("alice", "mallory"):
        _login_as(admin_client, who, PASSWORD)
        assert admin_client.patch(f"/api/v1/address-books/{guid}", json={"name": "hijack"}).status_code == 404
        assert admin_client.delete(f"/api/v1/address-books/{guid}").status_code == 404
        assert _share(admin_client, guid, who, 3).status_code == 404
        assert admin_client.delete(f"/api/v1/address-books/{guid}/shares/1").status_code == 404

    _login_as(admin_client, "admin", "adminpass123")
    assert admin_client.get("/api/v1/address-books").json()[1]["name"] == "Team"


def test_deleting_a_book_removes_its_entries_and_shares(admin_client):
    _make_user(admin_client, "alice")
    guid = _book(admin_client)["guid"]
    admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "111"})
    _share(admin_client, guid, "alice", 2)
    admin_client.delete(f"/api/v1/address-books/{guid}")

    _login_as(admin_client, "alice", PASSWORD)
    assert [b["name"] for b in admin_client.get("/api/v1/address-books").json()] == ["My address book"]
    assert admin_client.get(f"/api/v1/address-book?book={guid}").status_code == 404


def test_removing_a_share_that_does_not_exist_is_404(admin_client):
    guid = _book(admin_client)["guid"]
    assert admin_client.delete(f"/api/v1/address-books/{guid}/shares/999").status_code == 404


# -- entries in a shared book -------------------------------------------------


def test_entries_of_different_books_are_independent(admin_client):
    guid = _book(admin_client)["guid"]
    admin_client.post("/api/v1/address-book", json={"rustdesk_id": "111", "alias": "personal"})
    r = admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "111", "alias": "shared"})
    assert r.status_code == 201  # same id, different book

    personal = admin_client.get("/api/v1/address-book").json()
    shared = admin_client.get(f"/api/v1/address-book?book={guid}").json()
    assert [e["alias"] for e in personal] == ["personal"]
    assert [e["alias"] for e in shared] == ["shared"]
    dup = admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "111"})
    assert dup.status_code == 409


def test_rules_decide_what_a_shared_user_may_do_to_entries(admin_client):
    _make_user(admin_client, "reader")
    _make_user(admin_client, "writer")
    guid = _book(admin_client)["guid"]
    entry_id = admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "111"}).json()[
        "id"
    ]
    _share(admin_client, guid, "reader", 1)
    _share(admin_client, guid, "writer", 2)

    _login_as(admin_client, "reader", PASSWORD)
    assert [e["rustdesk_id"] for e in admin_client.get(f"/api/v1/address-book?book={guid}").json()] == ["111"]
    assert (
        admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "222"}).status_code == 403
    )
    assert admin_client.patch(f"/api/v1/address-book/{entry_id}", json={"alias": "x"}).status_code == 403
    assert admin_client.delete(f"/api/v1/address-book/{entry_id}").status_code == 403

    _login_as(admin_client, "writer", PASSWORD)
    r = admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "222", "tags": ["t"]})
    assert r.status_code == 201
    assert (
        admin_client.patch(
            f"/api/v1/address-book/{entry_id}", json={"alias": "edited", "tags": []}
        ).status_code
        == 200
    )
    assert admin_client.delete(f"/api/v1/address-book/{r.json()['id']}").status_code == 204


def test_a_stranger_gets_404_for_a_shared_books_entries(admin_client):
    _make_user(admin_client, "mallory")
    guid = _book(admin_client)["guid"]
    entry_id = admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "111"}).json()[
        "id"
    ]

    _login_as(admin_client, "mallory", PASSWORD)
    assert admin_client.get(f"/api/v1/address-book?book={guid}").status_code == 404
    assert (
        admin_client.post(f"/api/v1/address-book?book={guid}", json={"rustdesk_id": "x"}).status_code == 404
    )
    assert admin_client.patch(f"/api/v1/address-book/{entry_id}", json={"alias": "x"}).status_code == 404
    assert admin_client.delete(f"/api/v1/address-book/{entry_id}").status_code == 404

    _login_as(admin_client, "admin", "adminpass123")
    assert admin_client.get(f"/api/v1/address-book?book={guid}").json()[0]["alias"] is None


def test_an_entry_in_someone_elses_personal_book_stays_hidden_even_from_admins(admin_client):
    _make_user(admin_client, "alice")
    _login_as(admin_client, "alice", PASSWORD)
    entry_id = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "777"}).json()["id"]
    alice_guid = admin_client.get("/api/v1/address-books").json()[0]["guid"]

    _login_as(admin_client, "admin", "adminpass123")
    assert admin_client.get(f"/api/v1/address-book?book={alice_guid}").status_code == 404
    assert admin_client.patch(f"/api/v1/address-book/{entry_id}", json={"alias": "x"}).status_code == 404


def test_notes_are_stored_and_returned(admin_client):
    r = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "111", "note": "the print server"})
    assert r.json()["note"] == "the print server"
    r = admin_client.patch(f"/api/v1/address-book/{r.json()['id']}", json={"note": "moved", "tags": []})
    assert r.json()["note"] == "moved"


def test_entry_tags_come_back_with_hex_colors(admin_client):
    entry = admin_client.post("/api/v1/address-book", json={"rustdesk_id": "111", "tags": ["prod"]}).json()
    assert entry["tags"] == [{"name": "prod", "color": "#64748b"}]


def test_book_changes_are_audit_logged_without_secrets(admin_client):
    _make_user(admin_client, "alice")
    guid = _book(admin_client, "Team")["guid"]
    _share(admin_client, guid, "alice", 2)
    admin_client.delete(f"/api/v1/address-books/{guid}")

    logs = admin_client.get("/api/v1/admin/audit-logs").json()["items"]
    actions = {item["action"] for item in logs}
    assert {"address_book_created", "address_book_shared", "address_book_deleted"} <= actions
    shared = next(i for i in logs if i["action"] == "address_book_shared")
    assert shared["detail"] == {"book": "Team", "username": "alice", "rule": 2}
