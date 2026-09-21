"""Client strategies: named sets of RustDesk client options pushed to devices.

The mechanism is the RustDesk client's own (`hbbs_http/sync.rs`, present in
1.4.9 and `master`): every heartbeat carries the `modified_at` the client last
stored, and a heartbeat response with a `modified_at` and a
`strategy.config_options` map makes the client store that `modified_at` and
apply every option in the map. An option with an empty value is removed from
the client's own config, i.e. reset to its default.

Two consequences shape this module:

* Only options listed in `OPTIONS` can be pushed. It is a deliberate allow-list
  of permission and behaviour switches - never server addresses, keys,
  passwords or IP whitelists - so a strategy can neither redirect a fleet to
  another server nor lock people out.
* A strategy is written into the client's `Config` options (`handle_config_options`),
  so only keys the client itself keeps there (`KEYS_SETTINGS` in
  `libs/base/src/config/keys.rs`) can work. Keys the client reads from its local,
  built-in or per-session stores are ignored on the client even if pushed; they are
  listed in `RETIRED_KEYS` and are not offered.
* Every response carries *all* catalog keys (unset ones empty), so removing an
  option from a strategy, or unassigning it, resets the option on the client
  instead of leaving the last pushed value behind.
"""

from __future__ import annotations

import datetime
import json
import time
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rustdesk_api.models.device import Device
from rustdesk_api.models.group import Group
from rustdesk_api.models.strategy import Strategy

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 500


class StrategyError(Exception):
    pass


class InvalidStrategy(StrategyError):
    pass


class NameTaken(StrategyError):
    pass


@dataclass(frozen=True)
class OptionSpec:
    key: str
    label: str
    section: str
    kind: str  # "bool" (Y/N), "choice" or "int"
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    help: str = ""


_ON_OFF = "Y = on, N = off; not set = the client's own default."

OPTIONS: tuple[OptionSpec, ...] = (
    # What a remote user may do on this machine.
    OptionSpec(
        "access-mode",
        "Access mode",
        "Incoming permissions",
        "choice",
        ("custom", "full", "view"),
        help="full = every permission on, view = view only, custom = the switches below.",
    ),
    OptionSpec("enable-keyboard", "Keyboard and mouse", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-clipboard", "Clipboard", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-file-transfer", "File transfer", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-audio", "Audio", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-camera", "Camera", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-terminal", "Terminal", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-tunnel", "TCP tunneling", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-remote-restart", "Restart remotely", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-record-session", "Session recording", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-block-input", "Block user input", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-privacy-mode", "Privacy mode", "Incoming permissions", "bool", help=_ON_OFF),
    OptionSpec("enable-remote-printer", "Remote printer", "Incoming permissions", "bool", help=_ON_OFF),
    # How connections are approved.
    OptionSpec(
        "approve-mode",
        "Approve connections by",
        "Approval",
        "choice",
        ("password", "click"),
        help="Empty = both a password and a click are accepted.",
    ),
    OptionSpec(
        "verification-method",
        "Password type",
        "Approval",
        "choice",
        ("use-temporary-password", "use-permanent-password"),
        help="Empty = both temporary and permanent passwords are accepted.",
    ),
    OptionSpec(
        "allow-auto-disconnect",
        "Disconnect when idle",
        "Approval",
        "bool",
        help="Y = end a session after the idle time below.",
    ),
    OptionSpec("auto-disconnect-timeout", "Idle time (minutes)", "Approval", "int", minimum=1, maximum=1440),
    OptionSpec(
        "allow-only-conn-window-open",
        "Accept connections only while the window is open",
        "Approval",
        "bool",
        help="Y = refuse connections while the client window is closed. Installed clients only.",
    ),
    OptionSpec(
        "enable-trusted-devices",
        "Remember trusted devices (2FA)",
        "Approval",
        "bool",
        help="On = a peer that passed two-factor sign-in can be trusted and skip it next time.",
    ),
    OptionSpec(
        "allow-numeric-one-time-password",
        "Numeric temporary passwords",
        "Approval",
        "bool",
        help="Y = temporary passwords are digits only.",
    ),
    OptionSpec(
        "temporary-password-length",
        "Temporary password length",
        "Approval",
        "choice",
        ("6", "8", "10"),
    ),
    # A controlling client sending something its session type does not allow.
    OptionSpec(
        "allow-scope-violation-close",
        "End a session that steps outside its scope",
        "Session safety",
        "bool",
        help="Y = close the session when the peer sends a message its session type does not allow.",
    ),
    OptionSpec(
        "allow-scope-violation-alarm",
        "Report a scope violation",
        "Session safety",
        "bool",
        help="Y = send an alarm to this server (shown under Logs > Alarms) the first time it happens.",
    ),
    # Client behaviour.
    OptionSpec(
        "allow-remote-config-modification",
        "Let a remote user change settings",
        "Client",
        "bool",
        help=_ON_OFF,
    ),
    OptionSpec("allow-remove-wallpaper", "Remove wallpaper while connected", "Client", "bool", help=_ON_OFF),
    OptionSpec("allow-auto-record-incoming", "Record incoming sessions", "Client", "bool", help=_ON_OFF),
    OptionSpec("enable-lan-discovery", "LAN discovery", "Client", "bool", help=_ON_OFF),
    OptionSpec("allow-auto-update", "Update automatically", "Client", "bool", help=_ON_OFF),
    OptionSpec(
        "keep-awake-during-incoming-sessions",
        "Keep the machine awake during incoming sessions",
        "Client",
        "bool",
        help="On unless N.",
    ),
    # Video encoding on the controlled machine.
    OptionSpec("enable-abr", "Adaptive bitrate", "Video", "bool", help=_ON_OFF),
    OptionSpec("enable-hwcodec", "Hardware video codec", "Video", "bool", help=_ON_OFF),
    OptionSpec("enable-directx-capture", "DirectX screen capture (Windows)", "Video", "bool", help=_ON_OFF),
)

# Keys an earlier version offered that a strategy cannot reach: the client reads
# them from its local config (`enable-check-update`, `allow-auto-record-outgoing`),
# its built-in settings (`one-way-*`) or a per-session view option
# (`lock_after_session_end`), none of which the heartbeat writes. A strategy saved
# with one of them still loads and saves; the value is dropped and never pushed.
RETIRED_KEYS = frozenset(
    {
        "one-way-clipboard-redirection",
        "one-way-file-transfer",
        "lock_after_session_end",
        "allow-auto-record-outgoing",
        "enable-check-update",
    }
)

CATALOG: dict[str, OptionSpec] = {spec.key: spec for spec in OPTIONS}


def _choices(spec: OptionSpec) -> list[str]:
    if spec.kind == "bool":
        return ["Y", "N"]
    return list(spec.choices) if spec.kind == "choice" else []


def catalog() -> list[dict]:
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "section": spec.section,
            "kind": spec.kind,
            "choices": _choices(spec),
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "help": spec.help,
        }
        for spec in OPTIONS
    ]


def validate_options(raw: object) -> dict[str, str]:
    """The cleaned option map. Empty values are dropped (an unset option is
    simply absent). Raises `InvalidStrategy` for anything not in the catalog or
    with a value the client would not understand."""
    if not isinstance(raw, dict):
        raise InvalidStrategy("The options must be an object of option name to value.")
    options: dict[str, str] = {}
    for key, value in raw.items():
        if key in RETIRED_KEYS:
            continue
        spec = CATALOG.get(key)
        if spec is None:
            raise InvalidStrategy(f"{key!r} is not an option a strategy may set.")
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise InvalidStrategy(f"The value of {key!r} must be text.")
        text = str(value).strip()
        if spec.kind == "bool" and text not in ("Y", "N"):
            raise InvalidStrategy(f"{key!r} must be Y or N.")
        if spec.kind == "choice" and text not in spec.choices:
            raise InvalidStrategy(f"{key!r} must be one of: {', '.join(spec.choices)}.")
        if spec.kind == "int":
            if not text.isdigit() or not spec.minimum <= int(text) <= spec.maximum:
                raise InvalidStrategy(
                    f"{key!r} must be a whole number from {spec.minimum} to {spec.maximum}."
                )
            text = str(int(text))
        options[key] = text
    return options


def _clean_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidStrategy("A strategy needs a name.")
    name = value.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise InvalidStrategy(f"The name is longer than {MAX_NAME_LENGTH} characters.")
    return name


def _clean_description(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_DESCRIPTION_LENGTH:
        raise InvalidStrategy(f"The description must be text of at most {MAX_DESCRIPTION_LENGTH} characters.")
    return value.strip() or None


def _next_version(previous: int = 0) -> int:
    """Microseconds since the epoch, forced above `previous` so two edits in the
    same tick still differ."""
    return max(int(time.time() * 1_000_000), previous + 1)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def list_strategies(db: Session) -> list[Strategy]:
    return list(db.execute(select(Strategy).order_by(func.lower(Strategy.name))).scalars())


def get_by_id(db: Session, strategy_id: int) -> Strategy | None:
    return db.get(Strategy, strategy_id)


def get_by_name(db: Session, name: str) -> Strategy | None:
    return db.execute(
        select(Strategy).where(func.lower(Strategy.name) == name.strip().lower())
    ).scalar_one_or_none()


def create_strategy(db: Session, *, name: object, description: object, options: object) -> Strategy:
    clean_name = _clean_name(name)
    if get_by_name(db, clean_name) is not None:
        raise NameTaken("A strategy with this name already exists.")
    strategy = Strategy(
        name=clean_name,
        description=_clean_description(description),
        config_options=json.dumps(validate_options(options), sort_keys=True),
        modified_at=_next_version(),
    )
    db.add(strategy)
    try:
        db.flush()
    except IntegrityError as exc:  # a concurrent create with the same name
        raise NameTaken("A strategy with this name already exists.") from exc
    return strategy


def update_strategy(
    db: Session, strategy: Strategy, *, name: object, description: object, options: object
) -> Strategy:
    clean_name = _clean_name(name)
    other = get_by_name(db, clean_name)
    if other is not None and other.id != strategy.id:
        raise NameTaken("A strategy with this name already exists.")
    clean_options = validate_options(options)
    changed = json.dumps(clean_options, sort_keys=True) != json.dumps(strategy.options, sort_keys=True)
    strategy.name = clean_name
    strategy.description = _clean_description(description)
    if changed:
        strategy.config_options = json.dumps(clean_options, sort_keys=True)
        strategy.modified_at = _next_version(strategy.modified_at)
    db.flush()
    return strategy


def delete_strategy(db: Session, strategy: Strategy) -> None:
    """Devices and groups that used it fall back to no strategy (the foreign
    keys are ON DELETE SET NULL); their clients are reset on their next
    heartbeat."""
    db.delete(strategy)


def usage_counts(db: Session, strategy_ids: list[int]) -> dict[int, tuple[int, int]]:
    """strategy id -> (devices assigned directly, groups assigned)."""
    if not strategy_ids:
        return {}
    devices: dict[int, int] = {}
    for strategy_id, count in db.execute(
        select(Device.strategy_id, func.count(Device.id))
        .where(Device.strategy_id.in_(strategy_ids))
        .group_by(Device.strategy_id)
    ):
        if strategy_id is not None:
            devices[strategy_id] = count
    groups: dict[int, int] = {}
    for strategy_id, count in db.execute(
        select(Group.strategy_id, func.count(Group.id))
        .where(Group.strategy_id.in_(strategy_ids))
        .group_by(Group.strategy_id)
    ):
        if strategy_id is not None:
            groups[strategy_id] = count
    return {sid: (devices.get(sid, 0), groups.get(sid, 0)) for sid in strategy_ids}


def effective_strategy(db: Session, device: Device) -> Strategy | None:
    """The device's own strategy, else its group's."""
    if device.strategy_id is not None:
        return db.get(Strategy, device.strategy_id)
    if device.group_id is not None:
        group_strategy_id = db.execute(
            select(Group.strategy_id).where(Group.id == device.group_id)
        ).scalar_one_or_none()
        if group_strategy_id is not None:
            return db.get(Strategy, group_strategy_id)
    return None


def heartbeat_fragment(db: Session, device: Device, client_modified_at: int | None) -> dict:
    """What to merge into the heartbeat response for this device: nothing when
    the client already has the current strategy."""
    strategy = effective_strategy(db, device)
    have = client_modified_at or 0
    if strategy is None:
        if have == 0:
            return {}
        # It once had one: reset everything we may have pushed.
        return {"modified_at": 0, "strategy": {"config_options": {key: "" for key in CATALOG}}}
    if strategy.modified_at == have:
        return {}
    options = strategy.options
    return {
        "modified_at": strategy.modified_at,
        "strategy": {"config_options": {key: options.get(key, "") for key in CATALOG}},
    }
