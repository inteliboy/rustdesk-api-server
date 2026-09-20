from rustdesk_api.security.tokens import generate_token, hash_token


def test_generate_token_is_random_and_unique():
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50


def test_hash_token_is_deterministic_and_not_reversible_looking():
    token = generate_token()
    h1 = hash_token(token)
    h2 = hash_token(token)
    assert h1 == h2
    assert h1 != token
    assert len(h1) == 64  # sha256 hex digest
