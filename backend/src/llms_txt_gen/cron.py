"""Calculate future cron occurrences in UTC."""

from datetime import UTC, datetime

from croniter import croniter


def next_cron_time(expression: str, *, after: datetime | None = None) -> datetime:
    """Return the first matching time strictly after the supplied time or now.

    Invalid expressions and expressions without a future match raise ValueError.
    """
    base = datetime.now(UTC) if after is None else after.astimezone(UTC)
    return croniter(expression, base).get_next(datetime)
