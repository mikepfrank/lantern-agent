"""Civil time shown to Lantern in the operator's private room."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


CENTRAL_ZONE = "America/Chicago"


def central_time(instant: datetime | str | None = None) -> str:
    if instant is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(instant, str):
        instant = datetime.fromisoformat(instant)
    if instant.utcoffset() is None:
        raise ValueError("Displaying Central time requires a timestamp with an offset")
    local = instant.astimezone(ZoneInfo(CENTRAL_ZONE))
    offset = local.strftime("%z")
    return f"{local:%Y-%m-%d %I:%M:%S %p %Z} (UTC{offset[:3]}:{offset[3:]})"
