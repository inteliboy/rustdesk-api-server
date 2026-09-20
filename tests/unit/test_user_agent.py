import pytest

from rustdesk_api.security.user_agent import audit_client_detail, summarize_user_agent

CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
EDGE_WIN = CHROME_WIN + " Edg/130.0.2849.68"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"
SAFARI_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Mobile/15E148 Safari/604.1"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)
OPERA = CHROME_WIN + " OPR/115.0.0.0"


@pytest.mark.parametrize(
    ("ua", "browser", "os_name"),
    [
        (CHROME_WIN, "Chrome 130", "Windows"),
        (EDGE_WIN, "Edge 130", "Windows"),
        (OPERA, "Opera 115", "Windows"),
        (FIREFOX_LINUX, "Firefox 131", "Linux"),
        (SAFARI_MAC, "Safari 17", "macOS"),
        (SAFARI_IPHONE, "Safari 17", "iOS"),
        (CHROME_ANDROID, "Chrome 130", "Android"),
        ("curl/8.5.0", "curl", None),
        ("python-requests/2.32.3", "python-requests", None),
        ("reqwest/0.11.24", "RustDesk client", None),
        ("something entirely unknown", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_summarize_user_agent(ua, browser, os_name):
    info = summarize_user_agent(ua)
    assert (info.browser, info.os) == (browser, os_name)


def test_audit_detail_omits_empty_values_and_truncates_the_raw_header():
    assert audit_client_detail(None) == {}
    detail = audit_client_detail(CHROME_WIN + "x" * 500)
    assert detail["browser"] == "Chrome 130"
    assert detail["os"] == "Windows"
    assert len(detail["user_agent"]) == 200


def test_summary_never_echoes_attacker_text():
    info = summarize_user_agent("<script>alert(1)</script> Chrome/9<img> Windows")
    assert info.browser == "Chrome 9"
    assert info.os == "Windows"
