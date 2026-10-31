"""Deletion routes and repository cascades against real Postgres."""

import asyncio
import os
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest
from fastapi.testclient import TestClient

from llms_txt_gen.database import database_connection
from llms_txt_gen.main import create_app
from llms_txt_gen.repositories import RequestRepository, RunRepository
from llms_txt_gen.repositories.errors import NoDataFound


@pytest.fixture
def deletion_client(run_database):
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def request_tree(deletion_client, run_database):
    request = deletion_client.post(
        "/llms-txt/requests",
        json={"url": "https://example.com/", "cron_expression": "0 9 * * 1"},
    ).json()
    request_id = UUID(request["id"])
    other_request_id = uuid4()
    run_database.execute(
        "INSERT INTO llms_txt_requests (id, url) VALUES (%s, 'https://other.example/')",
        (other_request_id,),
    )
    file_id, other_file_id = uuid4(), uuid4()
    run_database.execute(
        "INSERT INTO llms_txt_files (id, type, content) "
        "VALUES (%s, 'text/markdown', '# Docs'), (%s, 'text/markdown', '# Other')",
        (file_id, other_file_id),
    )
    run_id, pending_run_id, other_run_id = uuid4(), uuid4(), uuid4()
    run_database.execute(
        "INSERT INTO llms_txt_runs (id, request_id, status, file_id) "
        "VALUES (%s, %s, 'success', %s), (%s, %s, 'pending', NULL), "
        "(%s, %s, 'success', %s)",
        (
            run_id,
            request_id,
            file_id,
            pending_run_id,
            request_id,
            other_run_id,
            other_request_id,
            other_file_id,
        ),
    )
    return {
        "request_id": request_id,
        "job_id": int(request["job_id"]),
        "run_id": run_id,
        "pending_run_id": pending_run_id,
        "file_id": file_id,
        "other_request_id": other_request_id,
        "other_run_id": other_run_id,
        "other_file_id": other_file_id,
    }


@pytest.mark.parametrize("via_repository", [False, True])
def test_delete_request_cascades_without_deleting_other_requests(
    deletion_client, run_database, request_tree, via_repository
):
    request_id = request_tree["request_id"]
    if via_repository:

        async def delete():
            async with database_connection() as connection:
                await RequestRepository(connection).delete(request_id)

        anyio.run(delete)
    else:
        response = deletion_client.delete(f"/llms-txt/requests/{request_id}")
        assert response.status_code == 204
        assert response.content == b""

    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == [
        {"id": request_tree["other_request_id"]}
    ]
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == [
        {"id": request_tree["other_run_id"]}
    ]
    assert run_database.execute("SELECT id FROM llms_txt_files").fetchall() == [
        {"id": request_tree["other_file_id"]}
    ]
    assert run_database.execute("SELECT status FROM procrastinate_jobs").fetchone() == {
        "status": "cancelled"
    }


@pytest.mark.parametrize("job_status", ["todo", "doing", "succeeded", "failed", "missing", None])
def test_delete_request_handles_job_states(deletion_client, run_database, job_status):
    request = deletion_client.post(
        "/llms-txt/requests", json={"url": "https://example.com/"}
    ).json()
    if job_status is None:
        run_database.execute("UPDATE llms_txt_requests SET job_id = NULL")
    elif job_status == "missing":
        run_database.execute("DELETE FROM procrastinate_jobs")
    else:
        run_database.execute("UPDATE procrastinate_jobs SET status = %s", (job_status,))

    response = deletion_client.delete(f"/llms-txt/requests/{request['id']}")

    assert response.status_code == 204
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
    job = run_database.execute("SELECT status, abort_requested FROM procrastinate_jobs").fetchone()
    if job_status == "missing":
        assert job is None
    else:
        assert job == {
            "status": "cancelled" if job_status == "todo" else job_status or "todo",
            "abort_requested": job_status in ("todo", "doing"),
        }


@pytest.mark.parametrize("via_repository", [False, True])
def test_delete_run_deletes_its_file(deletion_client, run_database, request_tree, via_repository):
    request_id, run_id = request_tree["request_id"], request_tree["run_id"]
    if via_repository:

        async def delete():
            async with database_connection() as connection:
                await RunRepository(connection).delete(run_id)

        anyio.run(delete)
    else:
        response = deletion_client.delete(f"/llms-txt/requests/{request_id}/runs/{run_id}")
        assert response.status_code == 204
        assert response.content == b""

    assert (
        run_database.execute("SELECT id FROM llms_txt_runs WHERE id = %s", (run_id,)).fetchall()
        == []
    )
    assert run_database.execute("SELECT id FROM llms_txt_files").fetchall() == [
        {"id": request_tree["other_file_id"]}
    ]
    assert deletion_client.get(f"/llms-txt/requests/{request_id}").status_code == 200
    assert (
        deletion_client.get(
            f"/llms-txt/requests/{request_id}/runs/{request_tree['pending_run_id']}"
        ).status_code
        == 200
    )


def test_delete_run_without_file(deletion_client, run_database, request_tree):
    request_id, run_id = request_tree["request_id"], request_tree["pending_run_id"]
    response = deletion_client.delete(f"/llms-txt/requests/{request_id}/runs/{run_id}")
    assert response.status_code == 204
    assert (
        run_database.execute("SELECT id FROM llms_txt_runs WHERE id = %s", (run_id,)).fetchall()
        == []
    )
    assert len(run_database.execute("SELECT id FROM llms_txt_files").fetchall()) == 2


@pytest.mark.parametrize("resource", ["request", "run"])
def test_delete_rolls_back_on_file_delete_failure(
    deletion_client, run_database, request_tree, resource
):
    run_database.execute(
        "CREATE TABLE protected_files (file_id UUID REFERENCES llms_txt_files(id))"
    )
    run_database.execute("INSERT INTO protected_files VALUES (%s)", (request_tree["file_id"],))
    path = f"/llms-txt/requests/{request_tree['request_id']}"
    if resource == "run":
        path += f"/runs/{request_tree['run_id']}"

    assert deletion_client.delete(path).status_code == 500
    assert len(run_database.execute("SELECT id FROM llms_txt_requests").fetchall()) == 2
    assert len(run_database.execute("SELECT id FROM llms_txt_runs").fetchall()) == 3
    assert len(run_database.execute("SELECT id FROM llms_txt_files").fetchall()) == 2
    assert run_database.execute("SELECT status FROM procrastinate_jobs").fetchone() == {
        "status": "todo"
    }


def test_delete_run_checks_parent_request(deletion_client, run_database, request_tree):
    run_id = request_tree["run_id"]
    for request_id in (request_tree["other_request_id"], uuid4()):
        response = deletion_client.delete(f"/llms-txt/requests/{request_id}/runs/{run_id}")
        assert response.status_code == 404
    assert len(run_database.execute("SELECT id FROM llms_txt_runs").fetchall()) == 3
    assert len(run_database.execute("SELECT id FROM llms_txt_files").fetchall()) == 2


@pytest.mark.parametrize("resource", ["request", "run"])
def test_delete_missing_resource_returns_not_found(deletion_client, request_id, resource):
    path = f"/llms-txt/requests/{uuid4()}"
    if resource == "run":
        path = f"/llms-txt/requests/{request_id}/runs/{uuid4()}"
    assert deletion_client.delete(path).status_code == 404


@pytest.mark.parametrize("resource", ["request", "run"])
def test_delete_invalid_ids_returns_validation_error(deletion_client, request_id, resource):
    path = "/llms-txt/requests/not-a-uuid"
    if resource == "run":
        path = f"/llms-txt/requests/{request_id}/runs/not-a-uuid"
    assert deletion_client.delete(path).status_code == 422


@pytest.mark.parametrize("job_status", ["todo", "doing", "succeeded", "failed", "missing", None])
def test_delete_run_handles_job_states(deletion_client, request_id, run_database, job_status):
    run_id = deletion_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    if job_status is None:
        run_database.execute("UPDATE llms_txt_runs SET job_id = NULL")
    elif job_status == "missing":
        run_database.execute("DELETE FROM procrastinate_jobs")
    else:
        run_database.execute("UPDATE procrastinate_jobs SET status = %s", (job_status,))

    response = deletion_client.delete(f"/llms-txt/requests/{request_id}/runs/{run_id}")

    assert response.status_code == 204
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    job = run_database.execute("SELECT status, abort_requested FROM procrastinate_jobs").fetchone()
    if job_status == "missing":
        assert job is None
    else:
        assert job == {
            "status": "cancelled" if job_status == "todo" else job_status or "todo",
            "abort_requested": job_status in ("todo", "doing"),
        }


def test_delete_request_stops_associated_run_jobs(deletion_client, run_database):
    request_id = deletion_client.post(
        "/llms-txt/requests", json={"url": "https://example.com/"}
    ).json()["id"]
    for _ in range(2):
        deletion_client.post(f"/llms-txt/requests/{request_id}/runs")
    run_database.execute(
        "UPDATE procrastinate_jobs SET status = 'doing' "
        "WHERE id = (SELECT max(id) FROM procrastinate_jobs)"
    )

    assert deletion_client.delete(f"/llms-txt/requests/{request_id}").status_code == 204

    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute(
        "SELECT status, abort_requested FROM procrastinate_jobs ORDER BY id"
    ).fetchall() == [
        {"status": "cancelled", "abort_requested": True},
        {"status": "cancelled", "abort_requested": True},
        {"status": "doing", "abort_requested": True},
    ]


def test_run_creation_waits_for_request_deletion(run_database, request_id):
    async def scenario():
        async with database_connection() as deleting, database_connection() as creating:
            task = None
            try:
                async with deleting.transaction():
                    await RequestRepository(deleting).delete(request_id)
                    task = asyncio.create_task(
                        RunRepository(creating).create(request_id=request_id)
                    )
                    async with await psycopg.AsyncConnection.connect(
                        os.environ["DATABASE_URL"], autocommit=True
                    ) as observer:
                        async with asyncio.timeout(5):
                            while not task.done():
                                cursor = await observer.execute(
                                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                                    (creating.info.backend_pid,),
                                )
                                if (await cursor.fetchone())[0] == "Lock":
                                    break
                                await asyncio.sleep(0.01)
                        assert not task.done(), "Run creation bypassed the parent deletion lock"
                with pytest.raises(NoDataFound):
                    await task
            finally:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    anyio.run(scenario)
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []
