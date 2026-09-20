"""Periodic housekeeping run inside the application process.

Deliberately a plain asyncio task, not Celery/Redis (CLAUDE.md section 41), and
deliberately only for cleanup: nothing here is needed for the data to be
consistent, and every run is idempotent, so several server processes running
it at once is harmless.
"""

from __future__ import annotations

import asyncio
import logging

import psutil

from rustdesk_api.config import Settings
from rustdesk_api.db.database import get_session_factory
from rustdesk_api.services import retention
from rustdesk_api.services.server_metrics import MetricsSampler

logger = logging.getLogger(__name__)


def run_retention_once(settings: Settings) -> retention.PurgeResult:
    with get_session_factory()() as db:
        result = retention.purge_expired(db, settings)
    if result.logs_total or result.sessions:
        logger.info(
            "log retention removed %d audit log(s), %d connection log(s), %d file transfer log(s), "
            "%d alarm log(s), %d expired session(s)",
            result.audit_logs,
            result.connection_logs,
            result.file_transfer_logs,
            result.alarm_logs,
            result.sessions,
        )
    return result


async def retention_loop(settings: Settings) -> None:
    """Runs immediately, then every LOG_RETENTION_INTERVAL_HOURS, until cancelled.
    The blocking database work runs in a thread so it never stalls request
    handling; a failed run is logged and retried at the next interval."""
    interval = settings.log_retention_interval_hours * 3600
    while True:
        try:
            await asyncio.to_thread(run_retention_once, settings)
        except Exception:
            logger.exception("log retention run failed; retrying in %d hour(s)", interval // 3600)
        await asyncio.sleep(interval)


async def metrics_loop(sampler: MetricsSampler) -> None:
    """Feeds the Server tab's CPU/memory history until cancelled. The first
    reading comes after a second so the tab has something to show soon after a
    start; readings are cheap (a few syscalls), so they run inline."""
    await asyncio.sleep(min(1, sampler.interval_seconds))
    while True:
        try:
            sampler.sample()
        except (psutil.Error, OSError):
            logger.exception("server metrics sample failed; retrying")
        await asyncio.sleep(sampler.interval_seconds)
