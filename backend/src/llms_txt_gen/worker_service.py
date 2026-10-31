"""Supervise a container worker, its local Chromium browser, and health listener."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from urllib.request import ProxyHandler, build_opener

from dotenv import load_dotenv


class WorkerShutdown(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def chromium_executable() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return playwright.chromium.executable_path


def wait_for_browser(process: subprocess.Popen, endpoint: str, *, timeout: float = 30) -> None:
    """Do not consume jobs until Chromium exposes a usable CDP endpoint."""
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chromium exited during startup (exit code {process.returncode}).")
        try:
            with opener.open(f"{endpoint}/json/version", timeout=0.5) as response:
                if json.load(response).get("webSocketDebuggerUrl"):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Chromium was not ready at {endpoint} within {timeout:g} seconds.")


def stop_processes(processes: list[subprocess.Popen], *, timeout: float = 10) -> None:
    """Drain the worker before closing Chrome; kill stuck process groups on exit."""
    deadline = time.monotonic() + timeout
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        except ProcessLookupError:
            process.wait()


def run_worker(arguments: list[str]) -> int:
    load_dotenv()
    processes = []
    previous_handlers = {}

    def shutdown(signum, frame):
        raise WorkerShutdown(signum)

    with ExitStack() as resources:
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, shutdown)
            environment = os.environ.copy()

            def start(command):
                process = subprocess.Popen(command, env=environment, start_new_session=True)
                processes.append(process)
                return process

            browser = None
            if not environment.get("PLAYWRIGHT_CDP_URL"):
                profile = resources.enter_context(tempfile.TemporaryDirectory(prefix="chromium-"))
                endpoint = "http://127.0.0.1:9222"
                browser = start(
                    [
                        chromium_executable(),
                        "--headless=new",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--remote-debugging-address=127.0.0.1",
                        "--remote-debugging-port=9222",
                        f"--user-data-dir={profile}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-background-networking",
                        "about:blank",
                    ]
                )
                wait_for_browser(browser, endpoint)
                environment["PLAYWRIGHT_CDP_URL"] = endpoint
                print(f"Chromium ready at {endpoint}", flush=True)

            health_directory = resources.enter_context(
                tempfile.TemporaryDirectory(prefix="health-")
            )
            with open(os.path.join(health_directory, "index.html"), "w") as page:
                page.write("ok\n")
            health = start(
                [
                    sys.executable,
                    "-m",
                    "http.server",
                    environment.get("PORT", "10000"),
                    "--bind",
                    "0.0.0.0",
                    "--directory",
                    health_directory,
                ]
            )
            worker = start(
                [
                    "procrastinate",
                    "--log-level",
                    "info",
                    "--log-format",
                    environment.get(
                        "LOG_FORMAT", "%(asctime)s %(levelname)s [%(name)s] %(message)s"
                    ),
                    "--app=llms_txt_gen.jobs.app",
                    "worker",
                    *arguments,
                ]
            )
            while True:
                worker_exit = worker.poll()
                if worker_exit is not None:
                    return worker_exit if worker_exit >= 0 else 128 - worker_exit
                for name, process in (("Chromium", browser), ("Health listener", health)):
                    if process is not None and process.poll() is not None:
                        raise RuntimeError(
                            f"{name} exited unexpectedly (code {process.returncode})."
                        )
                time.sleep(0.2)
        except WorkerShutdown as shutdown_request:
            print("Stopping worker and managed services", flush=True)
            return 128 + shutdown_request.signum
        except (OSError, RuntimeError) as error:
            print(f"Worker service failed: {error}", file=sys.stderr, flush=True)
            return 1
        finally:
            stop_processes(processes)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(run_worker(sys.argv[1:]))
