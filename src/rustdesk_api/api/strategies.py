"""Client strategies (/api/v1/strategies/...): named sets of RustDesk client
options pushed to devices through their heartbeat.

Administrator-only throughout. A strategy changes how other people's machines
behave, so neither its content nor its assignment is something an ordinary
user can influence, even for a device they own.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rustdesk_api.api.deps import get_current_admin, verify_csrf
from rustdesk_api.db.database import get_db
from rustdesk_api.errors import ApiError
from rustdesk_api.models.strategy import Strategy
from rustdesk_api.models.user import User
from rustdesk_api.services import audit as audit_service
from rustdesk_api.services import devices as device_service
from rustdesk_api.services import groups as group_service
from rustdesk_api.services import strategies as strategy_service

router = APIRouter(prefix="/api/v1", tags=["strategies"])


class StrategyOut(BaseModel):
    id: int
    name: str
    description: str | None
    options: dict[str, str]
    modified_at: int
    is_default: bool = False
    device_count: int = 0
    group_count: int = 0
    created_at: datetime.datetime
    updated_at: datetime.datetime


class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=strategy_service.MAX_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=strategy_service.MAX_DESCRIPTION_LENGTH)
    # Validated against the catalog by the service, which explains what is wrong.
    options: dict = Field(default_factory=dict)


class AssignStrategyRequest(BaseModel):
    strategy_id: int | None = None


def _to_out(strategy: Strategy, counts: tuple[int, int] = (0, 0)) -> StrategyOut:
    return StrategyOut(
        id=strategy.id,
        name=strategy.name,
        description=strategy.description,
        options=strategy.options,
        modified_at=strategy.modified_at,
        is_default=strategy.is_default,
        device_count=counts[0],
        group_count=counts[1],
        created_at=strategy.created_at,
        updated_at=strategy.updated_at,
    )


def _get_or_404(db: Session, strategy_id: int) -> Strategy:
    strategy = strategy_service.get_by_id(db, strategy_id)
    if strategy is None:
        raise ApiError("STRATEGY_NOT_FOUND", "The requested strategy does not exist.", 404)
    return strategy


def _translate(exc: strategy_service.StrategyError) -> ApiError:
    if isinstance(exc, strategy_service.NameTaken):
        return ApiError("STRATEGY_NAME_TAKEN", str(exc), 409)
    return ApiError("INVALID_STRATEGY", str(exc), 422)


@router.get("/strategies/options")
def strategy_options(_admin: User = Depends(get_current_admin)) -> list[dict]:
    """What a strategy may set: every option, its type and allowed values."""
    return strategy_service.catalog()


@router.get("/strategies", response_model=list[StrategyOut])
def list_strategies(db: Session = Depends(get_db), _admin: User = Depends(get_current_admin)):
    strategies = strategy_service.list_strategies(db)
    counts = strategy_service.usage_counts(db, [s.id for s in strategies])
    return [_to_out(s, counts.get(s.id, (0, 0))) for s in strategies]


@router.post("/strategies", response_model=StrategyOut, status_code=201, dependencies=[Depends(verify_csrf)])
def create_strategy(
    payload: StrategyIn, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> StrategyOut:
    try:
        strategy = strategy_service.create_strategy(
            db, name=payload.name, description=payload.description, options=payload.options
        )
    except strategy_service.StrategyError as exc:
        raise _translate(exc) from exc
    audit_service.record(
        db,
        action="strategy_created",
        actor_id=admin.id,
        target_type="strategy",
        target_id=strategy.id,
        detail={"name": strategy.name},
    )
    db.commit()
    return _to_out(strategy)


@router.get("/strategies/{strategy_id}", response_model=StrategyOut)
def get_strategy(
    strategy_id: int, db: Session = Depends(get_db), _admin: User = Depends(get_current_admin)
) -> StrategyOut:
    strategy = _get_or_404(db, strategy_id)
    return _to_out(strategy, strategy_service.usage_counts(db, [strategy.id])[strategy.id])


@router.put("/strategies/{strategy_id}", response_model=StrategyOut, dependencies=[Depends(verify_csrf)])
def update_strategy(
    strategy_id: int,
    payload: StrategyIn,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> StrategyOut:
    strategy = _get_or_404(db, strategy_id)
    try:
        strategy_service.update_strategy(
            db, strategy, name=payload.name, description=payload.description, options=payload.options
        )
    except strategy_service.StrategyError as exc:
        raise _translate(exc) from exc
    audit_service.record(
        db,
        action="strategy_updated",
        actor_id=admin.id,
        target_type="strategy",
        target_id=strategy.id,
        detail={"name": strategy.name},
    )
    db.commit()
    return _to_out(strategy, strategy_service.usage_counts(db, [strategy.id])[strategy.id])


@router.delete("/strategies/{strategy_id}", status_code=204, dependencies=[Depends(verify_csrf)])
def delete_strategy(
    strategy_id: int, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)
) -> None:
    strategy = _get_or_404(db, strategy_id)
    name = strategy.name
    strategy_service.delete_strategy(db, strategy)
    audit_service.record(
        db,
        action="strategy_deleted",
        actor_id=admin.id,
        target_type="strategy",
        target_id=strategy_id,
        detail={"name": name},
    )
    db.commit()


class DefaultStrategyRequest(BaseModel):
    is_default: bool


@router.put(
    "/strategies/{strategy_id}/default", response_model=StrategyOut, dependencies=[Depends(verify_csrf)]
)
def set_default_strategy(
    strategy_id: int,
    payload: DefaultStrategyRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> StrategyOut:
    """The strategy for devices that have none of their own and none through their group."""
    strategy = _get_or_404(db, strategy_id)
    if payload.is_default:
        strategy_service.set_default(db, strategy)
    elif strategy.is_default:
        strategy_service.set_default(db, None)
    audit_service.record(
        db,
        action="strategy_default_set" if payload.is_default else "strategy_default_cleared",
        actor_id=admin.id,
        target_type="strategy",
        target_id=strategy.id,
        detail={"name": strategy.name},
    )
    db.commit()
    return _to_out(strategy, strategy_service.usage_counts(db, [strategy.id])[strategy.id])


def _strategy_or_none(db: Session, strategy_id: int | None) -> Strategy | None:
    return None if strategy_id is None else _get_or_404(db, strategy_id)


@router.put("/devices/{device_id}/strategy", dependencies=[Depends(verify_csrf)])
def assign_device_strategy(
    device_id: int,
    payload: AssignStrategyRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> dict:
    device = device_service.get_by_id(db, device_id)
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND", "The requested device does not exist.", 404)
    strategy = _strategy_or_none(db, payload.strategy_id)
    device.strategy_id = strategy.id if strategy else None
    audit_service.record(
        db,
        action="strategy_assigned",
        actor_id=admin.id,
        target_type="device",
        target_id=device.id,
        detail={"rustdesk_id": device.rustdesk_id, "strategy": strategy.name if strategy else None},
    )
    db.commit()
    return {"strategy_id": device.strategy_id}


@router.put("/groups/{group_id}/strategy", dependencies=[Depends(verify_csrf)])
def assign_group_strategy(
    group_id: int,
    payload: AssignStrategyRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> dict:
    group = group_service.get_by_id(db, group_id)
    if group is None:
        raise ApiError("GROUP_NOT_FOUND", "The requested group does not exist.", 404)
    strategy = _strategy_or_none(db, payload.strategy_id)
    group.strategy_id = strategy.id if strategy else None
    audit_service.record(
        db,
        action="strategy_assigned",
        actor_id=admin.id,
        target_type="group",
        target_id=group.id,
        detail={"name": group.name, "strategy": strategy.name if strategy else None},
    )
    db.commit()
    return {"strategy_id": group.strategy_id}
