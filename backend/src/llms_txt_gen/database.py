"""Database connections for repository operations."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()


@asynccontextmanager
async def database_connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """Disable autocommit; commit on normal exit and roll back on exceptions.

    Explicit transaction blocks still commit independently when no transaction is
    active, or use savepoints when the caller has already started a transaction.
    """
    async with await psycopg.AsyncConnection.connect(
        os.environ["DATABASE_URL"], autocommit=False, row_factory=dict_row
    ) as connection:
        yield connection


async def get_connection() -> AsyncIterator[psycopg.AsyncConnection]:
    async with database_connection() as connection:
        yield connection
