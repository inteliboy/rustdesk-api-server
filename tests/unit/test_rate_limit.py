from rustdesk_api.security.rate_limit import RateLimiter


def test_allows_up_to_max_attempts_then_blocks():
    limiter = RateLimiter(max_attempts=3, window_seconds=60)
    key = "1.2.3.4"
    assert limiter.allow(key) is True
    assert limiter.allow(key) is True
    assert limiter.allow(key) is True
    assert limiter.allow(key) is False


def test_different_keys_are_independent():
    limiter = RateLimiter(max_attempts=1, window_seconds=60)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False


def test_reset_clears_the_window():
    limiter = RateLimiter(max_attempts=1, window_seconds=60)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    limiter.reset("a")
    assert limiter.allow("a") is True
