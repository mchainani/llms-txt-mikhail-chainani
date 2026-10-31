"""Application connections commit successful work and roll back failures."""

import asyncio
from uuid import uuid4

import anyio
import pytest

from llms_txt_gen.database import database_connection
from llms_txt_gen.repositories import RequestRepository


def test_raw_writes_commit_only_when_connection_exits_successfully(run_database):
    request_id = uuid4()

    async def scenario():
        async with database_connection() as connection:
            assert connection.autocommit is False
            await connection.execute(
                "INSERT INTO llms_txt_requests (id, url) VALUES (%s, %s)",
                (request_id, "https://example.com/"),
            )
            assert (
                run_database.execute(
                    "SELECT id FROM llms_txt_requests WHERE id = %s", (request_id,)
                ).fetchone()
                is None
            )

    anyio.run(scenario)
    assert run_database.execute(
        "SELECT url FROM llms_txt_requests WHERE id = %s", (request_id,)
    ).fetchone() == {"url": "https://example.com/"}


@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
def test_connection_failure_rolls_back_raw_and_nested_repository_writes(run_database, error_type):
    async def scenario():
        with pytest.raises(error_type):
            async with database_connection() as connection:
                await connection.execute(
                    "INSERT INTO llms_txt_requests (id, url) VALUES (%s, %s)",
                    (uuid4(), "https://example.com/first"),
                )
                await RequestRepository(connection).create(url="https://example.com/second")
                raise error_type("abort transaction")

    anyio.run(scenario)
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
