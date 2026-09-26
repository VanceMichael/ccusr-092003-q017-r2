"""时间工具：统一校验带偏移量的 ISO 8601 字符串。"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso8601(value: object, field: str = "occurred_at") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间：{value}")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须携带时区偏移：{value}")
    return parsed


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sort_key(iso_value: str) -> float:
    return datetime.fromisoformat(iso_value).timestamp()
