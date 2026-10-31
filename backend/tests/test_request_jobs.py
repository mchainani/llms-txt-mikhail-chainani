"""Request jobs create runs and enqueue generation atomically."""

from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

import anyio
import pytest
from procrastinate import JobContext
from test_runs import run_queue as run_queue
from test_runs import upstream as upstream

from llms_txt_gen.cron import next_cron_time
from llms_txt_gen.database import database_connection
from llms_txt_gen.jobs import app
from llms_txt_gen.jobs.request_worker import process_request
from llms_txt_gen.repositories import RequestRepository
from llms_txt_gen.repositories.errors import NoDataFound


@pytest.fixture
def request_context():
    return JobContext(
        app=app,
        job=process_request.configure().make_new_job(),
        start_timestamp=0.0,
        abort_reason=lambda: None,
    )


def enqueue_request(request_id):
    async def enqueue():
        async with database_connection() as connection:
            await process_request.configure(connection=connection).defer_async(
                request_id=str(request_id)
            )

    anyio.run(enqueue)


def create_scheduled_request(cron_expression="0 0 1 1 *"):
    async def create():
        async with database_connection() as connection:
            request = await RequestRepository(connection).create(
                url="https://example.com/", cron_expression=cron_expression
            )
            return {"id": str(request.id), "job_id": request.job_id}

    return anyio.run(create)


def test_request_job_creates_pending_run_and_generation_job(
    request_id, run_database, request_context
):
    async def process():
        await process_request(request_context, request_id=str(request_id))

    anyio.run(process)
    run = run_database.execute("SELECT * FROM llms_txt_runs").fetchone()
    assert run["request_id"] == request_id
    assert run["status"] == "pending"
    assert run["file_id"] is None
    assert run["created_at"].utcoffset() is not None
    assert run["timestamp"].utcoffset() is not None
    job = run_database.execute("SELECT * FROM procrastinate_jobs").fetchone()
    assert job["task_name"] == "process_llms_txt_run"
    assert job["args"] == {"run_id": str(run["id"])}
    assert job["lock"] == f"llms-txt-run:{run['id']}"
    assert job["status"] == "todo"
    assert run["job_id"] == str(job["id"])


def test_request_job_for_missing_request_does_not_create_run_or_job(run_database, request_context):
    async def process():
        with pytest.raises(NoDataFound):
            await process_request(request_context, request_id=str(uuid4()))

    anyio.run(process)
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


def test_worker_processes_initial_request_then_generates_run(run_database, upstream, run_queue):
    request = create_scheduled_request(cron_expression=None)
    run_queue()
    run = run_database.execute("SELECT * FROM llms_txt_runs").fetchone()
    assert str(run["request_id"]) == request["id"]
    assert run["status"] == "success"
    file = run_database.execute(
        "SELECT * FROM llms_txt_files WHERE id = %s", (run["file_id"],)
    ).fetchone()
    assert file["type"] == "text/markdown"
    assert file["content"].startswith("# Example\n")
    jobs = run_database.execute("SELECT task_name, status FROM procrastinate_jobs").fetchall()
    assert {(job["task_name"], job["status"]) for job in jobs} == {
        ("process_llms_txt_request", "succeeded"),
        ("process_llms_txt_run", "succeeded"),
    }
    assert upstream == ["example.com"]


def test_request_job_enqueue_failure_rolls_back_run(request_id, run_database, upstream, run_queue):
    run_database.execute(
        "ALTER TABLE procrastinate_jobs ADD CONSTRAINT reject_generation "
        "CHECK (task_name <> 'process_llms_txt_run')"
    )
    enqueue_request(request_id)
    run_queue()
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM llms_txt_files").fetchall() == []
    assert run_database.execute("SELECT task_name, status FROM procrastinate_jobs").fetchall() == [
        {"task_name": "process_llms_txt_request", "status": "todo"}
    ]
    assert upstream == []


def test_cron_request_worker_creates_run_and_schedules_next_occurrence(
    run_database, upstream, run_queue
):
    request = create_scheduled_request()
    first_job_id = int(request["job_id"])
    run_queue()
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT job_id FROM llms_txt_requests").fetchone() == {
        "job_id": request["job_id"]
    }
    assert upstream == []

    run_database.execute(
        "UPDATE procrastinate_jobs SET scheduled_at = now() WHERE id = %s", (first_job_id,)
    )
    run_queue()
    stored = run_database.execute("SELECT * FROM llms_txt_requests").fetchone()
    assert stored["job_id"] != request["job_id"]
    next_job = run_database.execute(
        "SELECT * FROM procrastinate_jobs WHERE id = %s", (int(stored["job_id"]),)
    ).fetchone()
    assert next_job["task_name"] == "process_llms_txt_request"
    assert next_job["args"] == {"request_id": request["id"]}
    assert next_job["status"] == "todo"
    assert next_job["scheduled_at"].month == 1
    assert next_job["scheduled_at"].day == 1
    runs = run_database.execute("SELECT * FROM llms_txt_runs").fetchall()
    assert len(runs) == 1
    assert str(runs[0]["request_id"]) == request["id"]
    assert runs[0]["status"] == "success"
    assert upstream == ["example.com"]

    # A replay after the transaction committed must not fork the schedule or create another run.
    run_database.execute(
        "UPDATE procrastinate_jobs SET status = 'todo', scheduled_at = now() WHERE id = %s",
        (first_job_id,),
    )
    run_queue()
    assert len(run_database.execute("SELECT id FROM llms_txt_runs").fetchall()) == 1
    assert len(run_database.execute("SELECT id FROM procrastinate_jobs").fetchall()) == 3
    assert run_database.execute("SELECT job_id FROM llms_txt_requests").fetchone() == {
        "job_id": stored["job_id"]
    }
    assert upstream == ["example.com"]


@pytest.mark.parametrize("failure_stage", ["enqueue", "save"])
def test_cron_renewal_failure_rolls_back_run_and_retries(
    run_database, upstream, run_queue, failure_stage
):
    request = create_scheduled_request()
    first_job_id = int(request["job_id"])
    if failure_stage == "enqueue":
        table = "procrastinate_jobs"
        check = f"task_name <> 'process_llms_txt_request' OR id = {first_job_id}"
    else:
        table = "llms_txt_requests"
        check = f"job_id = '{first_job_id}'"
    run_database.execute(f"ALTER TABLE {table} ADD CONSTRAINT reject_renewal CHECK ({check})")
    run_database.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    run_queue()
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM llms_txt_files").fetchall() == []
    assert run_database.execute("SELECT job_id FROM llms_txt_requests").fetchone() == {
        "job_id": request["job_id"]
    }
    assert run_database.execute(
        "SELECT id, status, attempts FROM procrastinate_jobs"
    ).fetchall() == [{"id": first_job_id, "status": "todo", "attempts": 1}]
    assert upstream == []

    run_database.execute(f"ALTER TABLE {table} DROP CONSTRAINT reject_renewal")
    run_database.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    run_queue()
    assert len(run_database.execute("SELECT id FROM llms_txt_runs").fetchall()) == 1
    assert (
        run_database.execute("SELECT job_id FROM llms_txt_requests").fetchone()["job_id"]
        != request["job_id"]
    )
    assert upstream == ["example.com"]


def test_exhausted_cron_schedule_rolls_back_run_and_retries(
    run_database, upstream, run_queue, monkeypatch
):
    request = create_scheduled_request("0 0 1 1 * 0 2027")
    monkeypatch.setattr(
        "llms_txt_gen.repositories.requests.next_cron_time",
        partial(next_cron_time, after=datetime(2027, 1, 1, tzinfo=UTC)),
    )
    run_database.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    run_queue()
    assert run_database.execute("SELECT job_id FROM llms_txt_requests").fetchone() == {
        "job_id": request["job_id"]
    }
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute(
        "SELECT task_name, status, attempts FROM procrastinate_jobs ORDER BY id"
    ).fetchall() == [
        {"task_name": "process_llms_txt_request", "status": "todo", "attempts": 1},
    ]
    assert upstream == []
