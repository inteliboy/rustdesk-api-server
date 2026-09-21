from __future__ import annotations

import hashlib
import hmac
import json
import smtplib

import httpx
import pytest

from rustdesk_api.config import Settings, clear_settings_cache, get_settings
from rustdesk_api.services import notifications
from rustdesk_api.services.notifications import Notification

WEBHOOK = "https://hooks.example.com/services/T000/B000/secret-token"
NTFY = "https://ntfy.example.com/private-topic-name"


@pytest.fixture(autouse=True)
def _fresh_state():
    notifications.reset_state()
    yield
    notifications.reset_state()


def _settings(monkeypatch, **env) -> Settings:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    clear_settings_cache()
    settings = get_settings()
    return settings


def _recorder(status=200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return seen, httpx.Client(transport=httpx.MockTransport(handler))


NOTE = Notification(
    "new_device", "New device registered", "Device 1001 (pc) registered.", {"rustdesk_id": "1001"}
)


def test_nothing_is_configured_by_default(settings):
    assert notifications.configured_channels(settings) == []
    assert notifications.dispatch("new_device", "t", "m", settings=settings) is False


def test_the_webhook_gets_the_event_and_a_signature_only_when_a_secret_is_set(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK, NOTIFY_WEBHOOK_SECRET="s3cret-value")
    seen, client = _recorder()

    assert notifications.deliver(configured, NOTE, client=client) == {"webhook": None}

    request = seen[0]
    body = json.loads(request.content)
    assert body["event"] == "new_device" and body["data"] == {"rustdesk_id": "1001"}
    # Chat tools read `text` (Slack, Mattermost) or `content` (Discord).
    assert "Device 1001" in body["text"] and "Device 1001" in body["content"]
    assert request.headers["x-rustdesk-api-event"] == "new_device"
    expected = hmac.new(b"s3cret-value", request.content, hashlib.sha256).hexdigest()
    assert request.headers["x-rustdesk-api-signature"] == f"sha256={expected}"


def test_an_unsigned_webhook_carries_no_signature_header(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK)
    seen, client = _recorder()
    notifications.deliver(configured, NOTE, client=client)
    assert "x-rustdesk-api-signature" not in seen[0].headers


def test_ntfy_gets_the_message_as_text_with_a_title_and_a_bearer_token(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_NTFY_URL=NTFY, NOTIFY_NTFY_TOKEN="tk_abcdef")
    seen, client = _recorder()
    alarm = Notification("client_alarm", "Security alarm on a device", "Device 7 reported something.")

    assert notifications.deliver(configured, alarm, client=client) == {"ntfy": None}

    request = seen[0]
    assert request.content == b"Device 7 reported something."
    assert request.headers["title"] == "Security alarm on a device"
    assert request.headers["priority"] == "high"
    assert request.headers["authorization"] == "Bearer tk_abcdef"


def test_a_failure_names_the_reason_but_never_the_url_or_a_credential(settings, monkeypatch):
    configured = _settings(
        monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK, NOTIFY_NTFY_URL=NTFY, NOTIFY_NTFY_TOKEN="tk_abcdef"
    )
    _seen, client = _recorder(status=401)

    results = notifications.deliver(configured, NOTE, client=client)

    assert results == {"webhook": "the server answered HTTP 401", "ntfy": "the server answered HTTP 401"}
    assert "secret-token" not in json.dumps(results) and "private-topic" not in json.dumps(results)


def test_one_failing_channel_does_not_stop_the_others(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK, NOTIFY_NTFY_URL=NTFY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hooks.example.com":
            raise httpx.ConnectError(
                "connection refused for https://hooks.example.com/services/T000/B000/secret-token"
            )
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = notifications.deliver(configured, NOTE, client=client)
    assert results["ntfy"] is None
    # Only the exception's type is reported: its message can contain the URL.
    assert results["webhook"] == "ConnectError"


def test_email_is_sent_through_smtp_with_starttls_and_login(settings, monkeypatch):
    configured = _settings(
        monkeypatch,
        NOTIFY_SMTP_HOST="mail.example.com",
        NOTIFY_SMTP_USER="mailer",
        NOTIFY_SMTP_PASSWORD="mail-password",
        NOTIFY_EMAIL_FROM="rustdesk@example.com",
        NOTIFY_EMAIL_TO="a@example.com, b@example.com",
    )
    calls: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"], message.get_content()))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    assert notifications.configured_channels(configured) == ["email"]
    assert notifications.deliver(configured, NOTE) == {"email": None}
    assert [c[0] for c in calls] == ["connect", "starttls", "login", "send"]
    assert calls[0][1:] == ("mail.example.com", 587)
    assert calls[3][1] == "a@example.com, b@example.com"
    assert calls[3][2] == "[RustDesk API] New device registered"
    assert "Device 1001" in calls[3][3]


def test_a_refused_mail_login_is_reported_without_the_server_reply(settings, monkeypatch):
    configured = _settings(
        monkeypatch,
        NOTIFY_SMTP_HOST="mail.example.com",
        NOTIFY_SMTP_USER="mailer",
        NOTIFY_SMTP_PASSWORD="mail-password",
        NOTIFY_EMAIL_FROM="rustdesk@example.com",
        NOTIFY_EMAIL_TO="a@example.com",
    )

    class Refusing:
        def __init__(self, *_a, **_k): ...
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None): ...
        def login(self, *_a):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials for mailer")

    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    assert notifications.deliver(configured, NOTE) == {"email": "the mail server refused the login"}


def test_email_needs_a_sender_and_a_recipient(settings, monkeypatch):
    assert (
        notifications.configured_channels(_settings(monkeypatch, NOTIFY_SMTP_HOST="mail.example.com")) == []
    )


def test_only_enabled_events_are_dispatched(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK, NOTIFY_EVENTS="client_alarm")
    assert notifications.is_enabled(configured, "client_alarm")
    assert not notifications.is_enabled(configured, "new_device")
    assert notifications.dispatch("new_device", "t", "m", settings=configured) is False


def test_dispatch_queues_and_the_worker_delivers(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK)
    delivered: list[Notification] = []
    monkeypatch.setattr(notifications, "deliver", lambda s, note, **_k: delivered.append(note) or {})

    assert notifications.dispatch("new_device", "Title", "Message", data={"a": 1}, settings=configured)
    assert notifications.flush()
    assert [(n.event, n.title, n.message, n.data) for n in delivered] == [
        ("new_device", "Title", "Message", {"a": 1})
    ]


def test_the_same_alarm_is_sent_once_per_throttle_window(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK)
    monkeypatch.setattr(notifications, "deliver", lambda *_a, **_k: {})

    def send(key):
        return notifications.dispatch(
            "client_alarm", "t", "m", dedupe_key=key, throttle_seconds=300, settings=configured
        )

    assert send("dev1:0") is True
    assert send("dev1:0") is False  # the same device and alarm again
    assert send("dev2:0") is True  # another device is a different key
    notifications.flush()


def test_a_burst_is_capped_per_minute_and_the_next_message_says_how_many_were_dropped(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK, NOTIFY_MAX_PER_MINUTE="3")
    delivered: list[Notification] = []
    monkeypatch.setattr(notifications, "deliver", lambda s, note, **_k: delivered.append(note) or {})

    sent = [notifications.dispatch("new_device", "t", f"device {i}", settings=configured) for i in range(6)]
    assert sent == [True, True, True, False, False, False]
    notifications.flush()
    assert len(delivered) == 3

    # A minute later the window has emptied; the first message through mentions the loss.
    notifications._sent_times.clear()
    assert notifications.dispatch("new_device", "t", "late", settings=configured)
    notifications.flush()
    assert "3 other notification(s) were not sent" in delivered[-1].message


def test_targets_never_include_the_secret_part_of_a_url(settings, monkeypatch):
    configured = _settings(monkeypatch, NOTIFY_WEBHOOK_URL=WEBHOOK, NOTIFY_NTFY_URL=NTFY)
    assert notifications.describe_targets(configured) == {
        "webhook": "hooks.example.com",
        "ntfy": "ntfy.example.com",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NOTIFY_WEBHOOK_URL", "ftp://example.com/x"),
        ("NOTIFY_SMTP_SECURITY", "plain"),
        ("NOTIFY_EVENTS", "new_device,nonsense"),
    ],
)
def test_bad_settings_are_rejected_at_startup(settings, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    clear_settings_cache()
    with pytest.raises(ValueError):
        get_settings()
    monkeypatch.delenv(name)
    clear_settings_cache()


def test_a_rejected_url_is_not_echoed_in_the_error(settings, monkeypatch):
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "ftp://example.com/secret-path-token")
    clear_settings_cache()
    with pytest.raises(ValueError) as excinfo:
        get_settings()
    assert "secret-path-token" not in str(excinfo.value)
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL")
    clear_settings_cache()
