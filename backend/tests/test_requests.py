import json
from datetime import UTC, datetime
from functools import partial
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from llms_txt_gen.cron import next_cron_time
from llms_txt_gen.main import create_app
from llms_txt_gen.routers.models import FetchPageRequest


@pytest.mark.parametrize("limit", [1, 250, 500, None])
def test_max_pages_accepts_positive_integers_or_default(limit):
    payload = FetchPageRequest(url="https://example.com/", max_pages=limit)
    assert payload.max_pages == limit


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "250", {}, []])
def test_max_pages_rejects_invalid_values(limit):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FetchPageRequest(url="https://example.com/", max_pages=limit)


@pytest.mark.parametrize("limit", [1, 500])
def test_request_page_limit_persists_with_timeout(requests_client, run_database, limit):
    response = requests_client.post(
        "/llms-txt/requests",
        json={"url": "https://example.com/", "max_pages": limit, "fetch_timeout_seconds": 120},
    )
    assert response.status_code == 201
    record = response.json()
    assert json.loads(record["configs"]) == {"max_pages": limit, "fetch_timeout_seconds": 120}
    stored = run_database.execute(
        "SELECT configs FROM llms_txt_requests WHERE id = %s", (record["id"],)
    ).fetchone()
    assert stored["configs"] == record["configs"]


@pytest.fixture
def requests_client(run_database):
    with TestClient(create_app()) as client:
        yield client


@pytest.mark.parametrize("extra", [{}, {"cron_expression": None}, {"fetch_timeout_seconds": None}])
def test_create_request_without_cron_enqueues_immediate_request_job(
    requests_client, run_database, extra
):
    response = requests_client.post(
        "/llms-txt/requests", json={"url": "https://EXAMPLE.com/docs?z=2&a=1#intro", **extra}
    )

    assert response.status_code == 201
    record = response.json()
    assert set(record) == {
        "id",
        "url",
        "created_at",
        "timestamp",
        "job_id",
        "cron_expression",
        "configs",
    }
    assert record["cron_expression"] is None
    assert record["configs"] is None
    assert record["job_id"] is not None
    request_id = UUID(record["id"])
    assert record["url"] == "https://example.com/docs?a=1&z=2"
    for field in ("created_at", "timestamp"):
        assert datetime.fromisoformat(record[field]).utcoffset() is not None
    stored = run_database.execute(
        "SELECT * FROM llms_txt_requests WHERE id = %s", (request_id,)
    ).fetchone()
    assert stored["url"] == record["url"]
    assert stored["job_id"] == record["job_id"]
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    jobs = run_database.execute("SELECT * FROM procrastinate_jobs").fetchall()
    assert len(jobs) == 1
    assert record["job_id"] == str(jobs[0]["id"])
    assert jobs[0]["task_name"] == "process_llms_txt_request"
    assert jobs[0]["args"] == {"request_id": str(request_id)}
    assert jobs[0]["scheduled_at"] is None
    assert jobs[0]["status"] == "todo"
    assert run_database.execute("SELECT id FROM llms_txt_files").fetchall() == []

    with TestClient(create_app()) as another_client:
        fetched = another_client.get(f"/llms-txt/requests/{request_id}")
    assert fetched.status_code == 200
    assert fetched.json() == record


def test_create_request_with_cron_enqueues_next_occurrence(
    requests_client, run_database, monkeypatch
):
    monkeypatch.setattr(
        "llms_txt_gen.repositories.requests.next_cron_time",
        partial(next_cron_time, after=datetime(2026, 10, 30, 9, tzinfo=UTC)),
    )
    response = requests_client.post(
        "/llms-txt/requests",
        json={"url": "https://example.com/", "cron_expression": "0 9 * * 1"},
    )
    assert response.status_code == 201
    record = response.json()
    assert "status" not in record
    assert record["cron_expression"] == "0 9 * * 1"
    job = run_database.execute(
        "SELECT * FROM procrastinate_jobs WHERE id = %s", (int(record["job_id"]),)
    ).fetchone()
    assert record["job_id"] == str(job["id"])
    assert job["task_name"] == "process_llms_txt_request"
    assert job["args"] == {"request_id": record["id"]}
    assert job["status"] == "todo"
    assert job["scheduled_at"] == datetime(2026, 11, 2, 9, tzinfo=UTC)
    stored = run_database.execute("SELECT * FROM llms_txt_requests").fetchone()
    assert stored["job_id"] == record["job_id"]
    assert stored["cron_expression"] == record["cron_expression"]
    assert "status" not in stored
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == [
        {"id": job["id"]}
    ]


@pytest.mark.parametrize("cron_expression", [None, "0 9 * * 1"])
def test_request_enqueue_failure_rolls_back_everything(run_database, cron_expression):
    run_database.execute("ALTER TABLE procrastinate_jobs ADD CONSTRAINT reject_jobs CHECK (false)")
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(
            "/llms-txt/requests",
            json={"url": "https://example.com/", "cron_expression": cron_expression},
        )
    assert response.status_code == 500
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


@pytest.mark.parametrize("cron_expression", [None, "0 9 * * 1"])
def test_request_job_id_update_failure_rolls_back_everything(run_database, cron_expression):
    run_database.execute(
        "ALTER TABLE llms_txt_requests ADD CONSTRAINT reject_job_id CHECK (job_id IS NULL)"
    )
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post(
            "/llms-txt/requests",
            json={"url": "https://example.com/", "cron_expression": cron_expression},
        )
    assert response.status_code == 500
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


def test_list_requests_returns_empty_list_and_all_records_newest_first(
    requests_client, run_database
):
    response = requests_client.get("/llms-txt/requests")
    assert response.status_code == 200
    assert response.json() == []
    first_id, second_id = uuid4(), uuid4()
    run_database.execute(
        """
        INSERT INTO llms_txt_requests (id, url, created_at, "timestamp")
        VALUES (%s, 'https://example.com/first',
                '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'),
               (%s, 'https://example.com/second',
                '2026-01-03T00:00:00Z', '2026-01-03T00:00:00Z')
        """,
        (first_id, second_id),
    )

    response = requests_client.get("/llms-txt/requests")
    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == [str(second_id), str(first_id)]
    assert rows[1] == {
        "id": str(first_id),
        "url": "https://example.com/first",
        "created_at": "2026-01-01T00:00:00Z",
        "timestamp": "2026-01-02T00:00:00Z",
        "job_id": None,
        "cron_expression": None,
        "configs": None,
    }


def test_get_unknown_request_returns_not_found(requests_client):
    response = requests_client.get(f"/llms-txt/requests/{uuid4()}")
    assert response.status_code == 404


def test_get_invalid_request_id_returns_validation_error(requests_client):
    response = requests_client.get("/llms-txt/requests/not-a-uuid")
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"url": "not-a-url"},
        {"url": "ftp://example.com/"},
        {"url": "https://example.com/", "status": "pending"},
        {"url": "https://example.com/", "cron_expression": "not cron"},
        {"url": "https://example.com/", "cron_expression": ""},
        {"url": "https://example.com/", "cron_expression": "99 * * * *"},
        {"url": "https://example.com/", "cron_expression": "0 0 31 2 *"},
        {"url": "https://example.com/", "cron_expression": 123},
    ],
)
def test_invalid_create_request_does_not_persist(requests_client, run_database, payload):
    response = requests_client.post("/llms-txt/requests", json=payload)
    assert response.status_code == 422
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
    assert run_database.execute("SELECT id FROM llms_txt_runs").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []


@pytest.mark.parametrize("timeout", [1, 120, 0.5])
def test_request_timeout_persists_as_json_text(requests_client, run_database, timeout):
    response = requests_client.post(
        "/llms-txt/requests",
        json={"url": "https://example.com/", "fetch_timeout_seconds": timeout},
    )
    assert response.status_code == 201
    record = response.json()
    stored = run_database.execute(
        "SELECT configs, pg_typeof(configs)::text AS type FROM llms_txt_requests WHERE id = %s",
        (record["id"],),
    ).fetchone()
    assert stored["type"] == "text"
    assert json.loads(stored["configs"]) == {"fetch_timeout_seconds": timeout}
    assert record["configs"] == stored["configs"]
    assert requests_client.get(f"/llms-txt/requests/{record['id']}").json() == record
    assert requests_client.get("/llms-txt/requests").json() == [record]


@pytest.mark.parametrize("timeout", [0, -1, 5e13, 1e20, 1e308, True, "120", "nan", "inf", {}, []])
def test_invalid_timeout_does_not_persist(requests_client, run_database, timeout):
    response = requests_client.post(
        "/llms-txt/requests",
        json={"url": "https://example.com/", "fetch_timeout_seconds": timeout},
    )
    assert response.status_code == 422
    assert run_database.execute("SELECT id FROM llms_txt_requests").fetchall() == []
    assert run_database.execute("SELECT id FROM procrastinate_jobs").fetchall() == []
