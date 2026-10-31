"""Repository contracts exercised against real Postgres, including row locking."""

import asyncio
import os
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from functools import partial
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest
from croniter import CroniterBadDateError
from procrastinate.exceptions import ConnectorException
from psycopg import sql
from psycopg.rows import dict_row

from llms_txt_gen.cron import next_cron_time
from llms_txt_gen.database import database_connection
from llms_txt_gen.repositories import FileRepository, RequestRepository, RunRepository
from llms_txt_gen.repositories.entities import File, Request, Run
from llms_txt_gen.repositories.errors import (
    CheckConstraintViolation,
    ForeignKeyViolation,
    NoDataFound,
    NotNullViolation,
    UniqueConstraintViolation,
)


@pytest.fixture(params=["requests", "runs", "files"])
def repository_case(request, run_database):
    if request.param == "runs":
        request_id = uuid4()
        run_database.execute(
            "INSERT INTO llms_txt_requests (id, url) VALUES (%s, 'https://example.com/')",
            (request_id,),
        )
        return RunRepository, {"request_id": request_id, "status": "pending"}, Run, "status"
    if request.param == "requests":
        return (
            RequestRepository,
            {"url": "https://example.com/", "job_id": "existing-job"},
            Request,
            "job_id",
        )
    return (
        FileRepository,
        {"type": "llms.txt", "content": "Café's documentation\n" * 10000},
        File,
        "content",
    )


def test_crud_and_list(run_database, repository_case):
    repository_type, values, entity_type, field = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            assert await repo.list() == []
            entity = await repo.create(**values)
            assert isinstance(entity, entity_type)
            assert getattr(entity, field) == values[field]
            assert await repo.read(entity.id) == entity
            assert await repo.list() == [entity]
            with pytest.raises(FrozenInstanceError):
                entity.id = uuid4()
            if not isinstance(entity, File):
                new_value = "123456789" if field == "job_id" else "new"
                updated = await repo.modify(
                    entity.id, lambda current: replace(current, **{field: new_value})
                )
                assert getattr(updated, field) == new_value
                assert getattr(await repo.read(entity.id), field) == new_value
                assert updated.created_at == entity.created_at
                assert updated.timestamp >= entity.timestamp
            if isinstance(entity, Run):
                assert entity.status == "pending"
                assert entity.file_id is None
            await repo.delete(entity.id)
            assert await repo.list() == []

    anyio.run(scenario)


@pytest.mark.parametrize("job_id", [None, "1234567890123456789"])
def test_request_job_ids_round_trip_and_can_be_updated(run_database, job_id):
    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            entity = await repo.create(url="https://example.com/", job_id=job_id)
            if job_id is None:
                assert entity.job_id is not None
            else:
                assert entity.job_id == job_id
            assert (await repo.read(entity.id)).job_id == entity.job_id
            assert (await repo.list())[0].job_id == entity.job_id
            updated = await repo.modify(
                entity.id, lambda current: replace(current, job_id="987654321")
            )
            assert updated.job_id == "987654321"
            assert (await repo.read(entity.id)).job_id == "987654321"
            cleared = await repo.modify(entity.id, lambda current: replace(current, job_id=None))
            assert cleared.job_id is None
            assert (await repo.read(entity.id)).job_id is None

    anyio.run(scenario)


@pytest.mark.parametrize("num_links", [None, 0, 7])
def test_run_link_count_round_trips_and_can_be_updated(run_database, request_id, num_links):
    async def scenario():
        async with database_connection() as connection:
            repo = RunRepository(connection)
            run = await repo.create(request_id=request_id, num_links=num_links)
            assert run.num_links == num_links
            assert (await repo.read(run.id)).num_links == num_links
            assert (await repo.list())[0].num_links == num_links
            assert (await repo.list_by_request_id(request_id))[0].num_links == num_links
            assert (await repo.get_latest_by_request_id(request_id)).num_links == num_links
            updated = await repo.modify(run.id, lambda current: replace(current, num_links=3))
            assert updated.num_links == 3
            assert (await repo.read(run.id)).num_links == 3
            cleared = await repo.modify(run.id, lambda current: replace(current, num_links=None))
            assert cleared.num_links is None
            assert (await repo.read(run.id)).num_links is None

    anyio.run(scenario)


def test_create_run_enqueues_job_and_saves_job_id(run_database, request_id):
    async def scenario():
        async with database_connection() as connection:
            repo = RunRepository(connection)
            run = await repo.create(request_id=request_id)
            assert run.job_id is not None
            assert run.num_links is None
            assert await repo.read(run.id) == run
        return run

    run = anyio.run(scenario)
    job = run_database.execute("SELECT * FROM procrastinate_jobs").fetchone()
    assert job["task_name"] == "process_llms_txt_run"
    assert job["args"] == {"run_id": str(run.id)}
    assert job["lock"] == f"llms-txt-run:{run.id}"
    assert job["status"] == "todo"
    assert run.job_id == str(job["id"])


@pytest.mark.parametrize(
    ("table", "check", "error_type"),
    [
        ("llms_txt_runs", "job_id IS NULL", CheckConstraintViolation),
        ("procrastinate_jobs", "false", ConnectorException),
    ],
)
def test_create_run_failure_rolls_back_row_and_job(
    run_database, request_id, table, check, error_type
):
    run_database.execute(
        sql.SQL("ALTER TABLE {} ADD CONSTRAINT reject_creation CHECK ({})").format(
            sql.Identifier(table), sql.SQL(check)
        )
    )

    async def scenario():
        async with database_connection() as connection:
            async with connection.transaction():
                with pytest.raises(error_type):
                    await RunRepository(connection).create(request_id=request_id)
                assert await RunRepository(connection).list() == []
                cursor = await connection.execute("SELECT id FROM procrastinate_jobs")
                assert await cursor.fetchall() == []

    anyio.run(scenario)
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


@pytest.mark.parametrize("cron_expression", [None, "0 9 * * 1"])
def test_request_schedule_round_trip(run_database, cron_expression):
    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            request = await repo.create(url="https://example.com/", cron_expression=cron_expression)
            assert request.cron_expression == cron_expression
            assert await repo.read(request.id) == request
            assert await repo.list() == [request]

    anyio.run(scenario)


@pytest.mark.parametrize("job_id", [None, "existing-job"])
@pytest.mark.parametrize(
    "cron_expression",
    ["", "not cron", "99 * * * *", "0 0 31 2 *", "0 0 1 1 * 0 2000"],
)
def test_create_request_rejects_invalid_cron_without_writes(run_database, job_id, cron_expression):
    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            async with connection.transaction():
                with pytest.raises(ValueError):
                    await repo.create(
                        url="https://example.com/", cron_expression=cron_expression, job_id=job_id
                    )
                assert await repo.list() == []
                cursor = await connection.execute("SELECT id FROM procrastinate_jobs")
                assert await cursor.fetchall() == []

    anyio.run(scenario)
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


@pytest.mark.parametrize("job_id", [None, "existing-job"])
@pytest.mark.parametrize("cron_expression", [None, "0 9 * * 1"])
def test_create_request_enqueues_at_next_cron_time_or_immediately_when_job_id_is_missing(
    run_database, monkeypatch, job_id, cron_expression
):
    monkeypatch.setattr(
        "llms_txt_gen.repositories.requests.next_cron_time",
        partial(next_cron_time, after=datetime(2026, 10, 30, 9, tzinfo=UTC)),
    )

    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            request = await repo.create(
                url="https://example.com/", cron_expression=cron_expression, job_id=job_id
            )
            assert await repo.read(request.id) == request
            return request

    request = anyio.run(scenario)
    jobs = run_database.execute("SELECT * FROM procrastinate_jobs").fetchall()
    if job_id is not None:
        assert request.job_id == job_id
        assert jobs == []
    else:
        assert len(jobs) == 1
        job = jobs[0]
        assert request.job_id == str(job["id"])
        assert job["task_name"] == "process_llms_txt_request"
        assert job["args"] == {"request_id": str(request.id)}
        assert job["lock"] == f"llms-txt-request:{request.id}"
        if cron_expression is None:
            assert job["scheduled_at"] is None
        else:
            assert job["scheduled_at"] == datetime(2026, 11, 2, 9, tzinfo=UTC)


@pytest.mark.parametrize("async_modifier", [False, True])
def test_modify_request_clearing_job_id_schedules_next_job(run_database, async_modifier):
    before = datetime.now(UTC)

    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            request = await repo.create(
                url="https://example.com/", cron_expression="0 9 * * 1", job_id="old-job"
            )

            def clear_job(current):
                return replace(current, job_id=None)

            async def clear_job_async(current):
                return clear_job(current)

            renewed = await repo.modify(
                request.id, clear_job_async if async_modifier else clear_job
            )
            assert renewed.job_id is not None
            assert renewed.job_id != "old-job"
            assert await repo.read(request.id) == renewed
            unchanged = await repo.modify(request.id, lambda current: current)
            assert unchanged == renewed
            return renewed

    renewed = anyio.run(scenario)
    jobs = run_database.execute("SELECT * FROM procrastinate_jobs").fetchall()
    assert len(jobs) == 1
    assert renewed.job_id == str(jobs[0]["id"])
    assert jobs[0]["task_name"] == "process_llms_txt_request"
    assert jobs[0]["args"] == {"request_id": str(renewed.id)}
    assert jobs[0]["scheduled_at"] > before
    scheduled = jobs[0]["scheduled_at"].astimezone(UTC)
    assert (scheduled.weekday(), scheduled.hour, scheduled.minute, scheduled.second) == (0, 9, 0, 0)


@pytest.mark.parametrize("operation", ["create", "modify"])
@pytest.mark.parametrize("failure_stage", ["enqueue", "save"])
def test_request_scheduling_failure_rolls_back_request_and_job(
    run_database, operation, failure_stage
):
    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            original = None
            if operation == "modify":
                original = await repo.create(
                    url="https://example.com/", cron_expression="0 9 * * 1", job_id="old-job"
                )
            if failure_stage == "enqueue":
                table, check, error_type = "procrastinate_jobs", "false", ConnectorException
            else:
                table = "llms_txt_requests"
                check = "job_id IS NULL" if operation == "create" else "job_id = 'old-job'"
                error_type = CheckConstraintViolation
            await connection.execute(
                sql.SQL("ALTER TABLE {} ADD CONSTRAINT reject_scheduling CHECK ({})").format(
                    sql.Identifier(table), sql.SQL(check)
                )
            )
            async with connection.transaction():
                with pytest.raises(error_type):
                    if operation == "create":
                        await repo.create(url="https://example.com/", cron_expression="0 9 * * 1")
                    else:
                        await repo.modify(
                            original.id, lambda current: replace(current, job_id=None)
                        )
                assert await repo.list() == ([] if original is None else [original])
                cursor = await connection.execute("SELECT id FROM procrastinate_jobs")
                assert await cursor.fetchall() == []

    anyio.run(scenario)


def test_modify_request_propagates_exhausted_cron_error(run_database, monkeypatch):
    async def scenario():
        async with database_connection() as connection:
            repo = RequestRepository(connection)
            request = await repo.create(
                url="https://example.com/",
                cron_expression="0 0 1 1 * 0 2027",
                job_id="old-job",
            )
            monkeypatch.setattr(
                "llms_txt_gen.repositories.requests.next_cron_time",
                partial(next_cron_time, after=datetime(2027, 1, 1, tzinfo=UTC)),
            )
            with pytest.raises(CroniterBadDateError):
                await repo.modify(request.id, lambda current: replace(current, job_id=None))
            assert await repo.read(request.id) == request

    anyio.run(scenario)
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


@pytest.mark.parametrize("operation", ["read", "modify", "delete"])
def test_missing_entity_raises_no_data_found(run_database, repository_case, operation):
    repository_type, _, _, _ = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            missing_id = uuid4()
            args = (missing_id, lambda current: current) if operation == "modify" else (missing_id,)
            if repository_type is FileRepository and operation == "modify":
                with pytest.raises(ValueError):
                    await repo.modify(missing_id, lambda current: current)
                return
            with pytest.raises(NoDataFound) as error:
                await getattr(repo, operation)(*args)
            assert error.value.entity_id == missing_id

    anyio.run(scenario)


def test_duplicate_id_has_domain_error_and_transaction_recovers(run_database, repository_case):
    repository_type, values, _, _ = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            async with connection.transaction():
                entity = await repo.create(**values)
                with pytest.raises(UniqueConstraintViolation) as error:
                    await repo.create(id=entity.id, **values)
                assert isinstance(error.value.__cause__, psycopg.errors.UniqueViolation)
                assert error.value.constraint_name is not None
                assert await repo.read(entity.id) == entity

    anyio.run(scenario)


@pytest.mark.parametrize("repository_case", ["requests", "runs"], indirect=True)
def test_modify_serializes_concurrent_changes(run_database, repository_case):
    repository_type, values, _, field = repository_case

    async def scenario():
        async with database_connection() as first, database_connection() as second:
            first_repo, second_repo = repository_type(first), repository_type(second)
            entity = await first_repo.create(**{**values, field: "initial"})
            locked = asyncio.Event()
            release = asyncio.Event()
            second_pid = second.info.backend_pid

            async def first_change(current):
                locked.set()
                await release.wait()
                return replace(current, **{field: getattr(current, field) + "-first"})

            first_task = asyncio.create_task(first_repo.modify(entity.id, first_change))
            await locked.wait()
            second_task = asyncio.create_task(
                second_repo.modify(
                    entity.id,
                    lambda current: replace(
                        current, **{field: getattr(current, field) + "-second"}
                    ),
                )
            )
            try:
                # Observe an actual Postgres lock wait, avoiding timing-based assertions.
                async with await psycopg.AsyncConnection.connect(
                    os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row
                ) as observer:
                    async with asyncio.timeout(5):
                        while True:
                            cursor = await observer.execute(
                                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                                (second_pid,),
                            )
                            if (await cursor.fetchone())["wait_event_type"] == "Lock":
                                break
                            await asyncio.sleep(0.01)
            finally:
                release.set()
                await asyncio.gather(first_task, second_task)
            assert getattr(await first_repo.read(entity.id), field) == "initial-first-second"

    anyio.run(scenario)


def test_modify_rolls_back_callback_side_effects(run_database):
    async def scenario():
        async with database_connection() as connection:
            runs, files = RunRepository(connection), FileRepository(connection)
            request = await RequestRepository(connection).create(url="https://example.com/")
            entity = await runs.create(request_id=request.id)

            async def fail(current):
                await files.create(type="llms.txt", content="must roll back")
                raise ValueError("callback failed")

            with pytest.raises(ValueError, match="callback failed"):
                await runs.modify(entity.id, fail)
            assert await files.list() == []
            assert await runs.read(entity.id) == entity

    anyio.run(scenario)


def test_modify_cannot_change_identity(run_database, repository_case):
    repository_type, values, _, _ = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            entity = await repo.create(**values)
            with pytest.raises(ValueError):
                await repo.modify(entity.id, lambda current: replace(current, id=uuid4()))
            assert await repo.read(entity.id) == entity

    anyio.run(scenario)


@pytest.mark.parametrize("repository_case", ["requests", "runs"], indirect=True)
def test_unchanged_modifier_does_not_write(run_database, repository_case):
    repository_type, values, _, _ = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            entity = await repo.create(**values)
            # A no-op must succeed even when writes to the table are forbidden.
            run_database.execute(
                f"ALTER TABLE {repo.table} ADD CONSTRAINT no_writes CHECK (false) NOT VALID"
            )
            assert await repo.modify(entity.id, lambda current: current) == entity
            assert await repo.read(entity.id) == entity

    anyio.run(scenario)


@pytest.mark.parametrize("repository_case", ["requests", "runs"], indirect=True)
def test_modify_constraint_failure_recovers_enclosing_transaction(run_database, repository_case):
    repository_type, values, _, field = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            entity = await repo.create(**values)
            run_database.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD CONSTRAINT reject_update CHECK ({} != 'rejected')"
                ).format(sql.Identifier(repo.table), sql.Identifier(field))
            )
            async with connection.transaction():
                with pytest.raises(CheckConstraintViolation):
                    await repo.modify(
                        entity.id, lambda current: replace(current, **{field: "rejected"})
                    )
                assert await repo.read(entity.id) == entity

    anyio.run(scenario)


def test_list_has_deterministic_tie_break_order(run_database, repository_case):
    repository_type, values, _, _ = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            async with connection.transaction():
                first = await repo.create(id=UUID(int=1), **values)
                second = await repo.create(id=UUID(int=2), **values)
            assert await repo.list() == [second, first]

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("constraint", "error_type"),
    [
        ("CHECK (false)", CheckConstraintViolation),
        ("FOREIGN KEY (id) REFERENCES llms_txt_runs(id)", ForeignKeyViolation),
    ],
)
def test_constraint_errors_are_translated(run_database, constraint, error_type):
    run_database.execute(f"ALTER TABLE llms_txt_files ADD CONSTRAINT test_constraint {constraint}")

    async def scenario():
        async with database_connection() as connection:
            with pytest.raises(error_type) as error:
                await FileRepository(connection).create(type="llms.txt", content="content")
            assert error.value.constraint_name == "test_constraint"
            assert isinstance(error.value.__cause__, psycopg.IntegrityError)

    anyio.run(scenario)


def test_not_null_error_is_translated(run_database):
    async def scenario():
        async with database_connection() as connection:
            with pytest.raises(NotNullViolation):
                await FileRepository(connection).create(type=None, content="content")

    anyio.run(scenario)


def test_file_content_cannot_be_null(run_database):
    async def scenario():
        async with database_connection() as connection:
            repo = FileRepository(connection)
            with pytest.raises(NotNullViolation):
                await repo.create(type="llms.txt", content=None)

    anyio.run(scenario)


@pytest.mark.parametrize("field", ["created_at", "timestamp"])
def test_modify_cannot_replace_timestamps(run_database, repository_case, field):
    from datetime import UTC, datetime

    repository_type, values, _, _ = repository_case

    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            entity = await repo.create(**values)
            with pytest.raises(ValueError):
                await repo.modify(
                    entity.id,
                    lambda current: replace(current, **{field: datetime(2000, 1, 1, tzinfo=UTC)}),
                )
            assert await repo.read(entity.id) == entity

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("limit", "expected_ids"),
    [(None, [2, 1, 10]), (0, []), (1, [2]), (2, [2, 1]), (10, [2, 1, 10])],
)
def test_runs_by_request_filters_and_orders_newest_first(run_database, limit, expected_ids):
    request_id, other_request_id = uuid4(), uuid4()
    run_database.execute(
        "INSERT INTO llms_txt_requests (id, url) VALUES (%s, 'https://example.com/'), (%s, 'https://other.example/')",
        (request_id, other_request_id),
    )
    run_database.execute(
        "INSERT INTO llms_txt_runs (id, request_id, status, created_at) VALUES "
        "(%s, %s, 'success', '2026-01-01T00:00:00Z'), "
        "(%s, %s, 'pending', '2026-01-02T00:00:00Z'), "
        "(%s, %s, 'failed', '2026-01-03T00:00:00Z'), "
        "(%s, %s, 'pending', '2026-01-04T00:00:00Z')",
        (
            UUID(int=10),
            request_id,
            UUID(int=1),
            request_id,
            UUID(int=2),
            request_id,
            UUID(int=999),
            other_request_id,
        ),
    )

    async def scenario():
        async with database_connection() as connection:
            repo = RunRepository(connection)
            rows = await repo.list_by_request_id(request_id, limit=limit)
            assert [row.id.int for row in rows] == expected_ids
            assert all(row.request_id == request_id for row in rows)
            latest = await repo.get_latest_by_request_id(request_id)
            assert latest.id == UUID(int=2)

    anyio.run(scenario)


def test_list_by_request_rejects_negative_limit(run_database):
    async def scenario():
        async with database_connection() as connection:
            with pytest.raises(ValueError, match="limit"):
                await RunRepository(connection).list_by_request_id(uuid4(), limit=-1)

    anyio.run(scenario)


def test_runs_by_request_without_matches(run_database):
    async def scenario():
        async with database_connection() as connection:
            repo = RunRepository(connection)
            request_id = uuid4()
            assert await repo.list_by_request_id(request_id) == []
            with pytest.raises(NoDataFound) as error:
                await repo.get_latest_by_request_id(request_id)
            assert error.value.entity_id == request_id

    anyio.run(scenario)


@pytest.mark.parametrize("async_modifier", [False, True])
def test_files_reject_modify_without_calling_callback(run_database, async_modifier):
    async def scenario():
        async with database_connection() as connection:
            repo = FileRepository(connection)
            entity = await repo.create(type="llms.txt", content="text")

            def modify(current):
                raise AssertionError("File modifier must not be called")

            async def modify_async(current):
                return modify(current)

            with pytest.raises(ValueError):
                await repo.modify(entity.id, modify_async if async_modifier else modify)
            assert await repo.read(entity.id) == entity

    anyio.run(scenario)


@pytest.mark.parametrize("async_modifier", [False, True])
@pytest.mark.parametrize(
    ("repository_type", "field", "value"),
    [
        (RequestRepository, "id", UUID(int=100)),
        (RequestRepository, "url", "https://other.example/"),
        (RequestRepository, "cron_expression", "0 9 * * 1"),
        (RequestRepository, "cron_expression", "invalid cron"),
        (RequestRepository, "created_at", datetime(2000, 1, 1, tzinfo=UTC)),
        (RunRepository, "id", UUID(int=100)),
        (RunRepository, "request_id", UUID(int=100)),
        (RunRepository, "created_at", datetime(2000, 1, 1, tzinfo=UTC)),
        (RunRepository, "job_id", "replacement-job"),
    ],
)
def test_modify_rejects_immutable_fields_and_rolls_back_callback_writes(
    run_database, request_id, repository_type, field, value, async_modifier
):
    async def scenario():
        async with database_connection() as connection:
            repo = repository_type(connection)
            values = (
                {"url": "https://example.com/"}
                if repository_type is RequestRepository
                else {"request_id": request_id}
            )
            entity = await repo.create(**values)
            cursor = await connection.execute("SELECT id FROM procrastinate_jobs ORDER BY id")
            original_jobs = await cursor.fetchall()

            def modify(current):
                return replace(current, **{field: value})

            async def modify_async(current):
                await FileRepository(connection).create(type="text/plain", content="roll back")
                return modify(current)

            with pytest.raises(ValueError, match=f"Cannot modify {field}"):
                await repo.modify(entity.id, modify_async if async_modifier else modify)
            assert await repo.read(entity.id) == entity
            assert await FileRepository(connection).list() == []
            cursor = await connection.execute("SELECT id FROM procrastinate_jobs ORDER BY id")
            assert await cursor.fetchall() == original_jobs

    anyio.run(scenario)
