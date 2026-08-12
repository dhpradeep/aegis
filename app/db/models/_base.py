from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
