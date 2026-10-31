"""Cron scheduling uses the next matching UTC instant, never the current one."""

from datetime import UTC, datetime

import pytest

from llms_txt_gen.cron import next_cron_time


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0 9 * * *", datetime(2026, 10, 31, 9, tzinfo=UTC)),
        ("0 9 * * 1", datetime(2026, 11, 2, 9, tzinfo=UTC)),
        ("0 0 29 2 *", datetime(2028, 2, 29, tzinfo=UTC)),
        ("@daily", datetime(2026, 10, 31, tzinfo=UTC)),
    ],
)
def test_next_cron_occurrence(expression, expected):
    assert next_cron_time(expression, after=datetime(2026, 10, 30, 9, tzinfo=UTC)) == expected
