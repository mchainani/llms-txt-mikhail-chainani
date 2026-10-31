import asyncio
import logging
import os
import socket
from datetime import datetime
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from procrastinate import PsycopgConnector

from llms_txt_gen.jobs import app as queue_app
from llms_txt_gen.main import create_app


@pytest.fixture
def runs_client(run_database):
    with TestClient(create_app()) as client:
        yield client


def test_create_run_persists_pending_row(request_id, runs_client, run_database):
    response = runs_client.post(f"/llms-txt/requests/{request_id}/runs")

    assert response.status_code == 201
    assert set(response.json()) == {"id"}
    run_id = UUID(response.json()["id"])
    row = run_database.execute("SELECT * FROM llms_txt_runs WHERE id = %s", (run_id,)).fetchone()
    request = run_database.execute(
        "SELECT * FROM llms_txt_requests WHERE id = %s", (row["request_id"],)
    ).fetchone()
    assert request["url"] == "https://example.com/"
    assert row["request_id"] == request_id
    assert len(run_database.execute("SELECT id FROM llms_txt_requests").fetchall()) == 1
    assert "url" not in row
    assert row["status"] == "pending"
    assert row["file_id"] is None
    assert row["num_links"] is None
    assert row["retry_count"] == 0
    assert "llms_txt" not in row
    assert row["created_at"].utcoffset() is not None
    assert row["timestamp"].utcoffset() is not None
    job = run_database.execute("SELECT * FROM procrastinate_jobs").fetchone()
    assert job is not None
    assert job["task_name"] == "process_llms_txt_run"
    assert job["args"] == {"run_id": str(run_id)}
    assert job["status"] == "todo"
    assert row["job_id"] == str(job["id"])

    # A fresh application and connection can read the committed run.
    with TestClient(create_app()) as another_client:
        fetched = another_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == str(run_id)
    assert fetched.json()["request_id"] == str(row["request_id"])
    assert fetched.json()["file_id"] is None
    assert fetched.json()["num_links"] is None
    assert fetched.json()["job_id"] == str(job["id"])
    assert "llms_txt" not in fetched.json()
    assert fetched.json()["retry_count"] == 0


def test_list_runs_returns_all_fields_and_empty_list(request_id, runs_client, run_database):
    assert runs_client.get(f"/llms-txt/requests/{request_id}/runs").json() == []
    first_id, second_id = uuid4(), uuid4()
    run_database.execute(
        """
        INSERT INTO llms_txt_runs
            (id, request_id, status, created_at, "timestamp")
        VALUES (%s, %s, 'success',
                '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'),
               (%s, %s, 'pending',
                '2026-01-03T00:00:00Z', '2026-01-03T00:00:00Z')
        """,
        (first_id, request_id, second_id, request_id),
    )

    response = runs_client.get(f"/llms-txt/requests/{request_id}/runs")
    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == [str(second_id), str(first_id)]
    first = rows[1]
    assert set(first) == {
        "id",
        "request_id",
        "status",
        "file_id",
        "created_at",
        "timestamp",
        "job_id",
        "num_links",
        "retry_count",
    }
    assert first["job_id"] is None
    assert first["num_links"] is None
    assert first["request_id"] == str(request_id)
    assert first["status"] == "success"
    assert first["file_id"] is None
    assert datetime.fromisoformat(first["created_at"]) == datetime.fromisoformat(
        "2026-01-01T00:00:00+00:00"
    )
    assert datetime.fromisoformat(first["timestamp"]) == datetime.fromisoformat(
        "2026-01-02T00:00:00+00:00"
    )
    assert runs_client.get(f"/llms-txt/requests/{request_id}/runs/{first_id}").json() == first


def test_get_unknown_run_returns_not_found(request_id, runs_client):
    response = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{uuid4()}")
    assert response.status_code == 404


def test_get_invalid_run_id_returns_validation_error(request_id, runs_client):
    response = runs_client.get(f"/llms-txt/requests/{request_id}/runs/not-a-uuid")
    assert response.status_code == 422


def test_runs_are_scoped_to_request_and_latest_uses_creation_time(
    request_id, runs_client, run_database
):
    first_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    second_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_database.execute(
        "UPDATE llms_txt_runs SET created_at = '2026-01-01T00:00:00Z', "
        '"timestamp" = now() WHERE id = %s',
        (first_id,),
    )
    other_request = runs_client.post(
        "/llms-txt/requests", json={"url": "https://other.example/"}
    ).json()["id"]
    other_run = runs_client.post(f"/llms-txt/requests/{other_request}/runs").json()["id"]

    listed = runs_client.get(f"/llms-txt/requests/{request_id}/runs")
    assert listed.status_code == 200
    assert [run["id"] for run in listed.json()] == [second_id, first_id]
    latest = runs_client.get(f"/llms-txt/requests/{request_id}/runs/latest")
    assert latest.status_code == 200
    assert latest.json() == listed.json()[0]
    assert runs_client.get(f"/llms-txt/requests/{request_id}/runs/{other_run}").status_code == 404


@pytest.mark.parametrize("suffix", ["", "/latest", "/" + str(uuid4())])
def test_get_runs_for_unknown_request_returns_not_found(runs_client, suffix):
    response = runs_client.get(f"/llms-txt/requests/{uuid4()}/runs{suffix}")
    assert response.status_code == 404


def test_latest_for_request_without_runs_returns_not_found(request_id, runs_client):
    response = runs_client.get(f"/llms-txt/requests/{request_id}/runs/latest")
    assert response.status_code == 404


@pytest.mark.parametrize("invalid_id, expected_status", [("not-a-uuid", 422), (str(uuid4()), 404)])
def test_create_run_for_invalid_or_missing_request_does_not_insert_rows(
    runs_client, run_database, invalid_id, expected_status
):
    response = runs_client.post(f"/llms-txt/requests/{invalid_id}/runs")
    assert response.status_code == expected_status
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


def test_enqueue_failure_rolls_back_run_and_job(run_database, request_id):
    run_database.execute("ALTER TABLE procrastinate_jobs ADD CONSTRAINT reject_jobs CHECK (false)")
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(f"/llms-txt/requests/{request_id}/runs")
    assert response.status_code == 500
    assert (
        run_database.execute("SELECT count(*) AS count FROM llms_txt_runs").fetchone()["count"] == 0
    )
    assert (
        run_database.execute("SELECT count(*) AS count FROM procrastinate_jobs").fetchone()["count"]
        == 0
    )
    assert (
        run_database.execute("SELECT count(*) AS count FROM llms_txt_requests").fetchone()["count"]
        == 1
    )


def test_job_id_update_failure_rolls_back_run_and_job(run_database, request_id):
    run_database.execute(
        "ALTER TABLE llms_txt_runs ADD CONSTRAINT reject_job_id CHECK (job_id IS NULL)"
    )
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(f"/llms-txt/requests/{request_id}/runs")
    assert response.status_code == 500
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == [
        {"id": request_id}
    ]


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host == "example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]
        return real_getaddrinfo(host, *args, **kwargs)

    def handle(request):
        calls.append(request.headers["host"])
        return httpx.Response(
            200,
            text='<title>Example</title><meta name="description" content="A useful site.">',
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    return calls


@pytest.fixture
def run_queue(run_database):
    def run(concurrency=1):
        async def work():
            connector = PsycopgConnector(conninfo=os.environ["DATABASE_URL"])
            with queue_app.replace_connector(connector):
                async with queue_app.open_async():
                    await queue_app.run_worker_async(
                        wait=False, install_signal_handlers=False, concurrency=concurrency
                    )

        anyio.run(work)

    return run


def test_worker_generates_logs_and_marks_success(
    request_id, runs_client, run_database, upstream, run_queue, caplog
):
    response = runs_client.post(f"/llms-txt/requests/{request_id}/runs")
    run_id = response.json()["id"]
    run_database.execute(
        'UPDATE llms_txt_runs SET "timestamp" = %s WHERE id = %s',
        ("2000-01-01T00:00:00Z", run_id),
    )

    with caplog.at_level(logging.INFO, logger="llms_txt_gen.jobs.run_worker"):
        run_queue()

    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()
    assert record["status"] == "success"
    assert record["num_links"] == 1
    assert datetime.fromisoformat(record["timestamp"]) > datetime.fromisoformat(
        "2000-01-01T00:00:00+00:00"
    )
    assert (
        run_database.execute("SELECT status FROM procrastinate_jobs").fetchone()["status"]
        == "succeeded"
    )
    assert upstream == ["example.com"]
    expected = (
        "# Example\n\n> A useful site.\n\n## Pages\n\n"
        "- [Example](https://example.com/): A useful site.\n"
    )
    assert "llms_txt" not in record
    file_id = UUID(record["file_id"])
    assert file_id != UUID(run_id)
    assert (
        run_database.execute(
            "SELECT content FROM llms_txt_files WHERE id = %s", (file_id,)
        ).fetchone()["content"]
        == expected
    )
    stored_file = run_database.execute(
        "SELECT * FROM llms_txt_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert stored_file["type"] == "text/markdown"
    assert stored_file["created_at"].utcoffset() is not None
    assert stored_file["timestamp"].utcoffset() is not None
    assert any(
        run_id in record.getMessage() and expected in record.getMessage()
        for record in caplog.records
    )

    # Simulate redelivery after the run's success was already committed.
    run_database.execute("UPDATE procrastinate_jobs SET status = 'todo'")
    run_queue()
    assert upstream == ["example.com"]
    assert (
        run_database.execute("SELECT count(*) AS count FROM llms_txt_files").fetchone()["count"]
        == 1
    )


@pytest.mark.parametrize(
    "timeout, default, expected_status", [(0.01, 600, "pending"), (2, 0.01, "success")]
)
def test_worker_uses_persisted_timeout(
    runs_client, run_database, upstream, monkeypatch, run_queue, timeout, default, expected_status
):
    from llms_txt_gen.crawling import fetcher

    async def handle(request):
        await anyio.sleep(0.05)
        return httpx.Response(
            200, text="<title>Slow page</title>", headers={"content-type": "text/html"}
        )

    monkeypatch.setattr(fetcher, "FETCH_TIMEOUT_SECONDS", default)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    response = runs_client.post(
        "/llms-txt/requests",
        json={"url": "https://example.com/", "fetch_timeout_seconds": timeout},
    )
    assert response.status_code == 201
    request_id = response.json()["id"]
    run_queue()
    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/latest").json()
    assert record["status"] == expected_status
    assert record["retry_count"] == 1
    files = run_database.execute("SELECT content FROM llms_txt_files").fetchall()
    if expected_status == "success":
        assert len(files) == 1
        assert files[0]["content"].startswith("# Slow page\n")
    else:
        assert files == []
        job = run_database.execute(
            "SELECT status, attempts FROM procrastinate_jobs "
            "WHERE task_name = 'process_llms_txt_run'"
        ).fetchone()
        assert job == {"status": "todo", "attempts": 1}


def test_worker_counts_all_retained_crawl_results(
    request_id, runs_client, run_database, upstream, monkeypatch, run_queue
):
    def handle(request):
        if request.url.path == "/":
            html = (
                '<title>Example</title><meta name="description" content="A useful site.">'
                '<a href="/guide">Guide</a><a href="/guide#top">Guide again</a>'
                '<a href="/about">About</a><a href="/missing">Missing</a>'
            )
        elif request.url.path == "/guide":
            html = '<title>Guide</title><meta name="description" content="Read this.">'
        elif request.url.path == "/about":
            html = "<title>About</title>"
        else:
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_queue()

    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()
    assert record["status"] == "success"
    assert record["num_links"] == 4
    assert runs_client.get(f"/llms-txt/requests/{request_id}/runs/latest").json() == record
    assert runs_client.get(f"/llms-txt/requests/{request_id}/runs").json() == [record]
    stored = run_database.execute(
        "SELECT num_links FROM llms_txt_runs WHERE id = %s", (run_id,)
    ).fetchone()
    assert stored["num_links"] == 4
    file = run_database.execute("SELECT content FROM llms_txt_files").fetchone()
    assert file["content"] == (
        "# Example\n\n> A useful site.\n\n## Pages\n\n"
        "- [Example](https://example.com/): A useful site.\n"
        "- [Guide](https://example.com/guide): Read this.\n"
        "\n## Optional\n\n- [About](https://example.com/about)\n"
        "- [/missing](https://example.com/missing)\n"
    )


def test_file_table_stores_large_unicode_content(run_database):
    file_id = uuid4()
    content = "# Docs\n\n" + "Café's documentation content.\n" * 10000
    run_database.execute(
        "INSERT INTO llms_txt_files (id, type, content) VALUES (%s, 'llms.txt', %s)",
        (file_id, content),
    )
    row = run_database.execute("SELECT * FROM llms_txt_files WHERE id = %s", (file_id,)).fetchone()
    assert row["id"] == file_id
    assert row["type"] == "llms.txt"
    assert row["content"] == content
    assert row["created_at"].utcoffset() is not None
    assert row["timestamp"].utcoffset() is not None


def test_file_write_failure_rolls_back_success(
    request_id, runs_client, run_database, upstream, run_queue
):
    run_database.execute("ALTER TABLE llms_txt_files ADD CONSTRAINT reject_content CHECK (false)")
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_queue()
    assert (
        runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["status"]
        == "pending"
    )
    assert (
        run_database.execute("SELECT count(*) AS count FROM llms_txt_files").fetchone()["count"]
        == 0
    )
    assert (
        run_database.execute("SELECT status FROM procrastinate_jobs").fetchone()["status"] == "todo"
    )


def test_status_changed_during_generation_skips_file_save(
    request_id, runs_client, run_database, upstream, monkeypatch, run_queue
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]

    def handle(request):
        run_database.execute("UPDATE llms_txt_runs SET status = 'failed' WHERE id = %s", (run_id,))
        return httpx.Response(
            200, text="<title>Example</title>", headers={"content-type": "text/html"}
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    run_queue()
    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()
    assert record["status"] == "failed"
    assert record["file_id"] is None
    assert record["num_links"] is None
    assert (
        run_database.execute("SELECT count(*) AS count FROM llms_txt_files").fetchone()["count"]
        == 0
    )
    assert (
        run_database.execute("SELECT status FROM procrastinate_jobs").fetchone()["status"]
        == "succeeded"
    )


def test_run_update_failure_rolls_back_inserted_file(
    request_id, runs_client, run_database, upstream, run_queue
):
    run_database.execute(
        "ALTER TABLE llms_txt_runs ADD CONSTRAINT remain_pending CHECK (status = 'pending')"
    )
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_queue()
    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()
    assert record["status"] == "pending"
    assert record["file_id"] is None
    assert record["num_links"] is None
    assert (
        run_database.execute("SELECT count(*) AS count FROM llms_txt_files").fetchone()["count"]
        == 0
    )
    assert (
        run_database.execute("SELECT status FROM procrastinate_jobs").fetchone()["status"] == "todo"
    )


def test_request_changed_during_generation_retries_without_saving_stale_content(
    request_id, runs_client, run_database, upstream, monkeypatch, run_queue
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    replacement_request_id = uuid4()
    run_database.execute(
        "INSERT INTO llms_txt_requests (id, url) VALUES (%s, 'https://example.com/updated')",
        (replacement_request_id,),
    )

    def handle(request):
        if request.url.path == "/":
            run_database.execute(
                "UPDATE llms_txt_runs SET request_id = %s WHERE id = %s",
                (replacement_request_id, run_id),
            )
        return httpx.Response(
            200,
            text=f"<title>{request.url.path}</title>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    run_queue()
    record = runs_client.get(f"/llms-txt/requests/{replacement_request_id}/runs/{run_id}").json()
    assert record["status"] == "pending"
    assert record["file_id"] is None
    assert run_database.execute("SELECT id FROM llms_txt_files").fetchall() == []
    job = run_database.execute("SELECT status, attempts FROM procrastinate_jobs").fetchone()
    assert job == {"status": "todo", "attempts": 1}

    run_database.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
    run_queue()
    assert (
        runs_client.get(f"/llms-txt/requests/{replacement_request_id}/runs/{run_id}").json()[
            "status"
        ]
        == "success"
    )
    saved = run_database.execute("SELECT content FROM llms_txt_files").fetchone()
    assert saved["content"].startswith("# /updated\n")


def test_concurrent_jobs_create_one_linked_file(
    request_id, runs_client, run_database, upstream, monkeypatch, run_queue
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    # Bypass the enqueue execution lock to exercise the row-lock protection itself.
    run_database.execute(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args)
        SELECT queue_name, task_name, args FROM procrastinate_jobs
        """
    )
    arrived = 0
    both_generating = asyncio.Event()

    async def handle(request):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_generating.set()
        async with asyncio.timeout(5):
            await both_generating.wait()
        return httpx.Response(
            200, text="<title>Example</title>", headers={"content-type": "text/html"}
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    run_queue(concurrency=2)
    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()
    assert record["status"] == "success"
    stored_files = run_database.execute("SELECT id FROM llms_txt_files").fetchall()
    assert stored_files == [{"id": UUID(record["file_id"])}]
    assert arrived == 2
    assert [
        row["status"]
        for row in run_database.execute("SELECT status FROM procrastinate_jobs").fetchall()
    ] == ["succeeded", "succeeded"]


@pytest.mark.parametrize("status", ["success", "failed", "processing"])
def test_worker_skips_non_pending_runs(
    request_id, runs_client, run_database, upstream, run_queue, status
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_database.execute("UPDATE llms_txt_runs SET status = %s WHERE id = %s", (status, run_id))
    run_queue()
    assert upstream == []
    assert (
        runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["status"] == status
    )


def test_missing_run_finishes_without_fetching(
    request_id, runs_client, run_database, upstream, run_queue
):
    runs_client.post(f"/llms-txt/requests/{request_id}/runs")
    run_database.execute("DELETE FROM llms_txt_runs")
    run_queue()
    assert upstream == []
    job = run_database.execute("SELECT status, attempts FROM procrastinate_jobs").fetchone()
    assert job["status"] == "succeeded"
    assert job["attempts"] == 1


@pytest.mark.parametrize("initial_retry_count", [0, 4])
def test_generation_failure_retries_after_30_seconds(
    request_id, runs_client, run_database, monkeypatch, upstream, run_queue, initial_retry_count
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_database.execute("UPDATE llms_txt_runs SET retry_count = %s", (initial_retry_count,))
    monkeypatch.setattr(
        httpx,
        "AsyncHTTPTransport",
        lambda **kwargs: httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    run_queue()
    assert (
        runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["status"]
        == "pending"
    )
    job = run_database.execute(
        """
        SELECT status, attempts,
               extract(epoch FROM scheduled_at - now()) AS retry_delay
        FROM procrastinate_jobs
        """
    ).fetchone()
    assert job["status"] == "todo"
    assert job["attempts"] == 1
    assert (
        runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["retry_count"]
        == initial_retry_count + 1
    )
    assert 25 < job["retry_delay"] <= 30

    if initial_retry_count == 4:
        # After the last generation attempt fails, finalize without another crawl.
        run_database.execute("UPDATE procrastinate_jobs SET scheduled_at = now()")
        run_queue()
        assert (
            runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["status"]
            == "failed"
        )
        assert (
            run_database.execute("SELECT status FROM procrastinate_jobs").fetchone()["status"]
            == "succeeded"
        )


@pytest.mark.parametrize(
    ("retry_count", "attempts", "expected_status", "expected_calls"),
    [
        (0, 100, "success", ["example.com"]),
        (4, 0, "success", ["example.com"]),
        (5, 0, "failed", []),
        (100, 0, "failed", []),
    ],
)
def test_worker_uses_persisted_retry_count_and_stops_above_five(
    request_id,
    runs_client,
    run_database,
    upstream,
    run_queue,
    retry_count,
    attempts,
    expected_status,
    expected_calls,
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_database.execute("UPDATE procrastinate_jobs SET attempts = %s", (attempts,))
    run_database.execute("UPDATE llms_txt_runs SET retry_count = %s", (retry_count,))
    run_database.execute(
        'UPDATE llms_txt_runs SET "timestamp" = %s WHERE id = %s',
        ("2000-01-01T00:00:00Z", run_id),
    )

    run_queue()

    record = runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()
    assert record["status"] == expected_status
    assert record["retry_count"] == retry_count + 1
    assert datetime.fromisoformat(record["timestamp"]) > datetime.fromisoformat(
        "2000-01-01T00:00:00+00:00"
    )
    assert upstream == expected_calls
    assert (
        run_database.execute("SELECT status FROM procrastinate_jobs").fetchone()["status"]
        == "succeeded"
    )


def test_retry_limit_does_not_overwrite_completed_run(
    request_id, runs_client, run_database, upstream, run_queue
):
    run_id = runs_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    run_database.execute("UPDATE procrastinate_jobs SET attempts = 6")
    run_database.execute("UPDATE llms_txt_runs SET status = 'success', retry_count = 5")
    run_queue()
    assert upstream == []
    assert (
        runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["status"]
        == "success"
    )

    assert (
        runs_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}").json()["retry_count"] == 5
    )
