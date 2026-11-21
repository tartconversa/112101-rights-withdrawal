"""Small shared helpers: canonical JSON, hashing and instant formatting."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any


UTC = dt.timezone.utc


def format_instant(value: dt.datetime) -> str:
    """Serialise an aware datetime as UTC ISO 8601 with a ``Z`` suffix."""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Stable JSON used for hashes and machine output."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=UTC).replace(microsecond=0)


def parse_instant_arg(value: str) -> dt.datetime:
    """Accept an ISO 8601 instant with an explicit offset (used by the CLI)."""

    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"不是合法的 ISO 8601 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"时间必须带时区偏移: {value!r}")
    return parsed.astimezone(UTC)
