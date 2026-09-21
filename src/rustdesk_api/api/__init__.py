from fastapi import APIRouter

from rustdesk_api.api import (
    account,
    address_book,
    address_book_management,
    address_book_protocol,
    admin,
    api_keys,
    auth,
    client_audit,
    data_transfer,
    device_tools,
    devices,
    enrollment,
    groups,
    health,
    heartbeat,
    ip_info,
    metrics,
    oidc,
    session_logs,
    shares,
    strategies,
    tags,
    two_factor,
    users,
    views,
    ws,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(metrics.router)
api_router.include_router(auth.rustdesk_router)
api_router.include_router(heartbeat.router)
api_router.include_router(client_audit.router)
api_router.include_router(address_book.router)
api_router.include_router(address_book_protocol.router)
api_router.include_router(auth.v1_router)
api_router.include_router(oidc.router)
api_router.include_router(oidc.v1_router)
api_router.include_router(account.router)
api_router.include_router(api_keys.router)
api_router.include_router(two_factor.router)
api_router.include_router(users.router)
api_router.include_router(device_tools.router)
api_router.include_router(devices.router)
api_router.include_router(views.router)
api_router.include_router(data_transfer.router)
api_router.include_router(enrollment.router)
api_router.include_router(shares.router)
api_router.include_router(groups.router)
api_router.include_router(strategies.router)
api_router.include_router(tags.router)
api_router.include_router(address_book_management.router)
api_router.include_router(address_book_management.books_router)
api_router.include_router(admin.router)
api_router.include_router(session_logs.router)
api_router.include_router(ip_info.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]
