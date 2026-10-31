"""Container worker startup, readiness, and shutdown without a real database/browser."""

import io
import json
import signal
import subprocess
from unittest.mock import Mock

import pytest


def test_browser_readiness_waits_for_cdp_not_just_a_listening_port(monkeypatch):
    from llms_txt_gen import worker_service

    process = Mock()
    process.poll.return_value = None
    responses = iter([{}, {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test"}])
    opener = Mock()
    opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(
        json.dumps(next(responses)).encode()
    )
    monkeypatch.setattr(worker_service, "build_opener", lambda *args: opener)
    monkeypatch.setattr(worker_service.time, "sleep", lambda seconds: None)
    worker_service.wait_for_browser(process, "http://127.0.0.1:9222", timeout=1)
    assert opener.open.call_count == 2


def test_browser_startup_failure_fails_before_starting_worker(monkeypatch):
    from llms_txt_gen import worker_service

    process = Mock()
    process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="Chromium exited"):
        worker_service.wait_for_browser(process, "http://127.0.0.1:9222", timeout=1)


def test_browser_readiness_has_a_deadline(monkeypatch):
    from llms_txt_gen import worker_service

    process = Mock()
    process.poll.return_value = None
    opener = Mock()
    opener.open.side_effect = OSError("Connection refused")
    monkeypatch.setattr(worker_service, "build_opener", lambda *args: opener)
    with pytest.raises(RuntimeError, match="ready"):
        worker_service.wait_for_browser(process, "http://127.0.0.1:9222", timeout=0.01)


def test_shutdown_stops_worker_before_browser_and_kills_stuck_process(monkeypatch):
    from llms_txt_gen import worker_service

    events = []
    worker = Mock(pid=101)
    browser = Mock(pid=102)
    worker.poll.return_value = browser.poll.return_value = None
    worker.wait.side_effect = [subprocess.TimeoutExpired("worker", 1), 0]
    browser.wait.return_value = 0
    monkeypatch.setattr(worker_service.os, "killpg", lambda pid, sig: events.append((pid, sig)))
    worker_service.stop_processes([browser, worker], timeout=0.01)
    assert events == [(101, signal.SIGTERM), (101, signal.SIGKILL), (102, signal.SIGTERM)]


def test_worker_arguments_and_external_browser_endpoint_are_preserved(monkeypatch):
    from llms_txt_gen import worker_service

    monkeypatch.setenv("PLAYWRIGHT_CDP_URL", "http://external-browser:9222")
    monkeypatch.setattr(worker_service, "load_dotenv", lambda: None)
    launched = []

    def spawn(command, **kwargs):
        process = Mock(pid=100 + len(launched))
        process.poll.return_value = 0 if command[0] == "procrastinate" else None
        launched.append((command, kwargs, process))
        return process

    monkeypatch.setattr(worker_service.subprocess, "Popen", spawn)
    monkeypatch.setattr(worker_service.os, "killpg", lambda *args: None)
    assert worker_service.run_worker(["--concurrency", "4"]) == 0
    assert len(launched) == 2  # Health listener and worker; external browser is not owned.
    command, options, _ = launched[-1]
    assert command[-3:] == ["worker", "--concurrency", "4"]
    assert options["env"]["PLAYWRIGHT_CDP_URL"] == "http://external-browser:9222"


def test_worker_starts_only_after_managed_chromium_is_ready(monkeypatch):
    from llms_txt_gen import worker_service

    monkeypatch.delenv("PLAYWRIGHT_CDP_URL", raising=False)
    monkeypatch.setattr(worker_service, "load_dotenv", lambda: None)
    monkeypatch.setattr(worker_service, "chromium_executable", lambda: "/installed/chromium")
    events = []

    def spawn(command, **kwargs):
        events.append(command)
        process = Mock(pid=100 + len(events))
        process.poll.return_value = 0 if command[0] == "procrastinate" else None
        return process

    monkeypatch.setattr(worker_service.subprocess, "Popen", spawn)
    monkeypatch.setattr(worker_service.os, "killpg", lambda *args: None)
    monkeypatch.setattr(worker_service, "wait_for_browser", lambda *args: events.append("ready"))
    assert worker_service.run_worker([]) == 0
    assert events[0][0] == "/installed/chromium"
    assert "--headless=new" in events[0]
    assert "--remote-debugging-address=127.0.0.1" in events[0]
    assert events[1] == "ready"
    assert events[-1][0] == "procrastinate"
