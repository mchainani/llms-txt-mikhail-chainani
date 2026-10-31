"""Shared isolated Postgres schema for persistence integration tests."""

import os
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row


@pytest.fixture
def run_database(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run Postgres integration tests")

    schema = f"test_runs_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            for path in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
                migration = path.read_text()
                schema_prefix = sql.Identifier(schema).as_string(connection) + "."
                connection.execute(migration.replace("public.", schema_prefix))
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            connection.execute(files("procrastinate").joinpath("sql/schema.sql").read_text())
            monkeypatch.setenv(
                "DATABASE_URL", make_conninfo(dsn, options=f"-c search_path={schema}")
            )
            yield connection
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def request_id(run_database):
    request_id = uuid4()
    run_database.execute(
        "INSERT INTO llms_txt_requests (id, url) VALUES (%s, 'https://example.com/')",
        (request_id,),
    )
    return request_id
