"""UTC clock for timestamps stored in naive DateTime columns."""
from datetime import datetime, timezone


def utcnow():
    """Return the current UTC time as a naive datetime.

    Every DateTime column stores naive UTC, so the tzinfo is dropped to keep
    comparisons with stored values valid. Replaces the deprecated
    datetime.datetime.utcnow().
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
