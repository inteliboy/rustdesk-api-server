from rustdesk_api.security.passwords import hash_password, verify_password


def test_hash_password_produces_argon2id_hash():
    h = hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")


def test_verify_password_accepts_correct_password():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True


def test_verify_password_rejects_wrong_password():
    h = hash_password("correct horse battery staple")
    assert verify_password("wrong password", h) is False


def test_verify_password_rejects_garbage_hash():
    assert verify_password("anything", "not-a-real-hash") is False
