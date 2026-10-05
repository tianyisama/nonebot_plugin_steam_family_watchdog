"""与 JavaScript ISO 时间格式兼容的辅助函数。"""

import json
from datetime import datetime, timezone


def iso_time(timestamp: float | None = None) -> str:
    value = datetime.now(timezone.utc) if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
