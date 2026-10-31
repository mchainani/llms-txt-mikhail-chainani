"""The HTTP server exposes persistence APIs without loading the crawler."""

import os
import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from llms_txt_gen.main import create_app


@pytest.mark.parametrize("operation", ["crawl", "format", "generate"])
def test_removed_crawling_endpoints_return_not_found(operation):
    path = f"/llms-txt/{operation}"
    with TestClient(create_app()) as client:
        assert client.post(path, json={}).status_code == 404
        assert path not in client.get("/openapi.json").json()["paths"]


def test_server_import_and_startup_do_not_load_crawlee():
    script = textwrap.dedent("""
        import importlib.abc
        import sys

        class RejectCrawlerImport(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(
                    fullname == prefix or fullname.startswith(prefix + ".")
                    for prefix in ("crawlee", "llms_txt_gen.crawling", "llms_txt_gen.generation")
                ):
                    raise AssertionError(f"Server loaded crawler module: {fullname}")

        sys.meta_path.insert(0, RejectCrawlerImport())
        from fastapi.testclient import TestClient
        from llms_txt_gen.main import create_app

        with TestClient(create_app()) as client:
            assert client.get("/docs").status_code == 200
            paths = client.get("/openapi.json").json()["paths"]
            assert set(paths) == {
                "/llms-txt/requests/{request_id}/runs",
                "/llms-txt/requests/{request_id}/runs/{run_id}",
                "/llms-txt/requests", "/llms-txt/requests/{request_id}",
                "/llms-txt/requests/{request_id}/runs/{run_id}/files",
            }
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "DATABASE_URL": "postgresql://unused@localhost/unused"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
