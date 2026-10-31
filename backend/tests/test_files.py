from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from llms_txt_gen.main import create_app


@pytest.fixture
def files_client(run_database):
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def stored_run_file(run_database, request_id):
    file_id, run_id = uuid4(), uuid4()
    content = "# Café docs\n\n" + "A document's content.\n" * 10000
    run_database.execute(
        """
        INSERT INTO llms_txt_files (id, type, content, created_at, "timestamp")
        VALUES (%s, 'llms.txt', %s, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
        """,
        (file_id, content),
    )
    run_database.execute(
        "INSERT INTO llms_txt_runs (id, request_id, status, file_id) "
        "VALUES (%s, %s, 'success', %s)",
        (run_id, request_id, file_id),
    )
    return run_id, file_id, content


def test_get_run_file_returns_content_and_metadata(files_client, request_id, stored_run_file):
    run_id, file_id, content = stored_run_file
    response = files_client.get(f"/llms-txt/requests/{request_id}/runs/{run_id}/files")
    assert response.status_code == 200
    assert response.json() == {
        "id": str(file_id),
        "type": "llms.txt",
        "content": content,
        "created_at": "2026-01-01T00:00:00Z",
        "timestamp": "2026-01-02T00:00:00Z",
    }


def test_run_without_file_returns_not_found(
    files_client, request_id, run_database, stored_run_file
):
    old_run_id, _, _ = stored_run_file
    run_database.execute(
        "UPDATE llms_txt_runs SET created_at = '2026-01-01T00:00:00Z' WHERE id = %s",
        (old_run_id,),
    )
    new_run_id = files_client.post(f"/llms-txt/requests/{request_id}/runs").json()["id"]
    response = files_client.get(f"/llms-txt/requests/{request_id}/runs/{new_run_id}/files")
    assert response.status_code == 404


def test_get_file_for_missing_run_returns_not_found(files_client, request_id):
    response = files_client.get(f"/llms-txt/requests/{request_id}/runs/{uuid4()}/files")
    assert response.status_code == 404


def test_get_file_checks_parent_request(files_client, stored_run_file):
    run_id, _, _ = stored_run_file
    other_request = files_client.post(
        "/llms-txt/requests", json={"url": "https://other.example/"}
    ).json()["id"]
    response = files_client.get(f"/llms-txt/requests/{other_request}/runs/{run_id}/files")
    assert response.status_code == 404
    response = files_client.get(f"/llms-txt/requests/{uuid4()}/runs/{run_id}/files")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "request_selector, run_selector",
    [("not-a-uuid", str(uuid4())), (str(uuid4()), "not-a-uuid"), (str(uuid4()), "latest")],
)
def test_get_file_rejects_invalid_ids(files_client, request_selector, run_selector):
    response = files_client.get(f"/llms-txt/requests/{request_selector}/runs/{run_selector}/files")
    assert response.status_code == 422
