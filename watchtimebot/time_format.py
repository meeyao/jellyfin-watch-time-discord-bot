from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def humanize_datetime(dt: datetime, tz: Optional[tzinfo] = None) -> str:
    if tz is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    if tz is not None and dt.tzinfo is not tz:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def format_discord_timestamp(dt: datetime, tz: Optional[tzinfo] = None, style: str = "F") -> str:
    """
    Render a datetime as a Discord timestamp token so Discord localizes it per user.
    """
    if tz is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    if tz is not None and dt.tzinfo is not tz:
        dt = dt.astimezone(tz)
    if tz is None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix_ts = int(dt.timestamp())
    return f"<t:{unix_ts}:{style}>"
