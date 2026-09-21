"""Notifications to a webhook, an ntfy topic and/or e-mail.

The channels are configured through environment variables only (see Settings): a
webhook URL, an ntfy topic and an SMTP password are credentials, so they are neither
stored in the database nor shown in the WebUI, and never logged. `dispatch` is what
the rest of the application calls; it returns at once and a background thread does
the network work, so a slow or dead channel can never hold up a heartbeat or a login.

Events (NOTIFY_EVENTS chooses which are sent):

    device_offline           a device flagged "notify when offline" stopped reporting
    device_back_online       ... and is reporting again
    new_device               a device registered for the first time
    device_takeover_attempt  another install tried to take over a device's id
    client_alarm             a RustDesk client reported a security alarm
    account_locked           an account was locked after too many wrong passwords
    backup_failed            a scheduled database backup failed
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import queue
import smtplib
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from rustdesk_api.config import NOTIFY_EVENT_NAMES, Settings, get_settings

logger = logging.getLogger(__name__)

EVENTS = {
    "device_offline": "A device flagged for offline alerts stopped reporting",
    "device_back_online": "A device that was reported offline is reporting again",
    "new_device": "A device registered for the first time",
    "device_takeover_attempt": "Another install tried to take over a device's ID",
    "client_alarm": "A RustDesk client reported a security alarm",
    "account_locked": "An account was locked after too many wrong passwords",
    "backup_failed": "A scheduled database backup failed",
}
assert tuple(EVENTS) == NOTIFY_EVENT_NAMES

CHANNELS = ("webhook", "ntfy", "email")
_HIGH_PRIORITY = {"device_takeover_attempt", "client_alarm", "account_locked", "backup_failed"}


@dataclass(frozen=True)
class Notification:
    event: str
    title: str
    message: str
    # Structured facts for the webhook body (never secrets).
    data: dict = field(default_factory=dict)

    @property
    def high_priority(self) -> bool:
        return self.event in _HIGH_PRIORITY


def configured_channels(settings: Settings) -> list[str]:
    channels = []
    if settings.notify_webhook_url:
        channels.append("webhook")
    if settings.notify_ntfy_url:
        channels.append("ntfy")
    if settings.notify_smtp_host and settings.notify_email_from and settings.notify_email_recipients:
        channels.append("email")
    return channels


def is_enabled(settings: Settings, event: str) -> bool:
    return event in settings.notify_event_list and bool(configured_channels(settings))


def describe_targets(settings: Settings) -> dict[str, str]:
    """Where each configured channel sends to, without anything secret: the host of a
    URL (its path or query may be the credential) and the address list of an e-mail."""
    targets: dict[str, str] = {}
    if settings.notify_webhook_url:
        targets["webhook"] = urlsplit(settings.notify_webhook_url).hostname or "?"
    if settings.notify_ntfy_url:
        targets["ntfy"] = urlsplit(settings.notify_ntfy_url).hostname or "?"
    if "email" in configured_channels(settings):
        targets["email"] = (
            f"{len(settings.notify_email_recipients)} recipient(s) via {settings.notify_smtp_host}"
        )
    return targets


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _webhook(settings: Settings, note: Notification, client: httpx.Client) -> None:
    body = json.dumps(
        {
            "event": note.event,
            "title": note.title,
            "message": note.message,
            # Slack, Mattermost, Rocket.Chat and Google Chat read `text`; Discord reads `content`.
            "text": f"{note.title}: {note.message}",
            "content": f"**{note.title}**\n{note.message}",
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "server": settings.external_url,
            "data": note.data,
        },
        separators=(",", ":"),
    ).encode()
    headers = {"Content-Type": "application/json", "X-RustDesk-API-Event": note.event}
    if settings.notify_webhook_secret:
        digest = hmac.new(settings.notify_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-RustDesk-API-Signature"] = f"sha256={digest}"
    response = client.post(settings.notify_webhook_url, content=body, headers=headers)
    response.raise_for_status()


def _ntfy(settings: Settings, note: Notification, client: httpx.Client) -> None:
    headers = {
        "Title": note.title,
        "Priority": "high" if note.high_priority else "default",
        "Tags": "warning" if note.high_priority else "computer",
    }
    if settings.notify_ntfy_token:
        headers["Authorization"] = f"Bearer {settings.notify_ntfy_token}"
    response = client.post(settings.notify_ntfy_url, content=note.message.encode(), headers=headers)
    response.raise_for_status()


def _email(settings: Settings, note: Notification) -> None:
    message = EmailMessage()
    message["Subject"] = f"[RustDesk API] {note.title}"
    message["From"] = settings.notify_email_from
    message["To"] = ", ".join(settings.notify_email_recipients)
    message.set_content(f"{note.message}\n\n-- \n{settings.external_url}\n")
    timeout = settings.notify_timeout_seconds
    security = settings.notify_smtp_security
    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.notify_smtp_host, settings.notify_smtp_port, timeout=timeout, context=context
        )
    else:
        server = smtplib.SMTP(settings.notify_smtp_host, settings.notify_smtp_port, timeout=timeout)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if settings.notify_smtp_user:
            server.login(settings.notify_smtp_user, settings.notify_smtp_password)
        server.send_message(message)


def _failure_reason(exc: Exception) -> str:
    """What went wrong, without anything that could carry a credential: exception
    messages from httpx and smtplib can include the URL or the server's greeting."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"the server answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "timed out"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the mail server refused the login"
    return type(exc).__name__


def deliver(
    settings: Settings, note: Notification, *, client: httpx.Client | None = None
) -> dict[str, str | None]:
    """Sends `note` on every configured channel now (in the calling thread). Returns,
    per channel, None on success or a short reason on failure. A failing channel never
    stops the others."""
    results: dict[str, str | None] = {}
    channels = configured_channels(settings)
    own_client = client is None
    if own_client and ("webhook" in channels or "ntfy" in channels):
        # A redirect could carry the request (and a bearer token) to another host.
        client = httpx.Client(timeout=settings.notify_timeout_seconds, follow_redirects=False)
    try:
        for channel in channels:
            try:
                if channel == "webhook":
                    assert client is not None
                    _webhook(settings, note, client)
                elif channel == "ntfy":
                    assert client is not None
                    _ntfy(settings, note, client)
                else:
                    _email(settings, note)
                results[channel] = None
            except (httpx.HTTPError, smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                results[channel] = _failure_reason(exc)
                logger.warning("notification channel %s failed: %s", channel, results[channel])
    finally:
        if own_client and client is not None:
            client.close()
    return results


# ---------------------------------------------------------------------------
# Queue, rate limit and throttle
# ---------------------------------------------------------------------------

_QUEUE_LIMIT = 200
_queue: queue.Queue[tuple[Settings, Notification]] = queue.Queue(maxsize=_QUEUE_LIMIT)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None

_state_lock = threading.Lock()
_sent_times: deque[float] = deque()
_suppressed = 0
_throttle: dict[tuple[str, str], float] = {}


def _work() -> None:
    while True:
        settings, note = _queue.get()
        try:
            deliver(settings, note)
        except Exception:
            # The worker must survive whatever a channel throws; deliver() already
            # handles the errors it expects, so this is a bug being reported.
            logger.exception("sending a notification failed unexpectedly")
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="notifications", daemon=True)
            _worker.start()


def _admit(settings: Settings, event: str, dedupe_key: str | None, throttle_seconds: float) -> int | None:
    """Applies the per-key throttle and the global per-minute cap. Returns how many
    notifications were dropped since the last one that got through (to mention in it),
    or None when this one is not to be sent."""
    global _suppressed
    now = time.monotonic()
    with _state_lock:
        if dedupe_key is not None and throttle_seconds > 0:
            key = (event, dedupe_key)
            last = _throttle.get(key)
            if last is not None and now - last < throttle_seconds:
                return None
            if len(_throttle) > 1000:
                for stale in [k for k, t in _throttle.items() if now - t > 3600]:
                    del _throttle[stale]
            _throttle[key] = now
        while _sent_times and now - _sent_times[0] > 60:
            _sent_times.popleft()
        if len(_sent_times) >= settings.notify_max_per_minute:
            _suppressed += 1
            return None
        _sent_times.append(now)
        dropped, _suppressed = _suppressed, 0
        return dropped


def dispatch(
    event: str,
    title: str,
    message: str,
    *,
    data: dict | None = None,
    dedupe_key: str | None = None,
    throttle_seconds: float = 0,
    settings: Settings | None = None,
) -> bool:
    """Queues a notification and returns at once. Does nothing (returns False) when the
    event is not enabled or no channel is configured. `dedupe_key` with
    `throttle_seconds` sends at most one per key in that time, so a device that keeps
    raising the same alarm is one message."""
    settings = settings or get_settings()
    if not is_enabled(settings, event):
        return False
    dropped = _admit(settings, event, dedupe_key, throttle_seconds)
    if dropped is None:
        return False
    if dropped:
        message += f"\n({dropped} other notification(s) were not sent because of NOTIFY_MAX_PER_MINUTE.)"
    _ensure_worker()
    try:
        _queue.put_nowait((settings, Notification(event, title, message, data or {})))
    except queue.Full:
        logger.warning("the notification queue is full; dropped a %s notification", event)
        return False
    return True


def flush(timeout: float = 5.0) -> bool:
    """Waits until everything queued has been handled (tests)."""
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _queue.unfinished_tasks


def reset_state() -> None:
    """Forgets the rate limit and the throttle (tests)."""
    global _suppressed
    with _state_lock:
        _sent_times.clear()
        _throttle.clear()
        _suppressed = 0
