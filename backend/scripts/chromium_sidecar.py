"""Start disposable, localhost-only headless Chromium for the Playwright prototype."""

import argparse
import subprocess
import tempfile

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument(
        "--executable", help="Use an installed Chrome/Chromium instead of Playwright's"
    )
    args = parser.parse_args()
    with (
        sync_playwright() as playwright,
        tempfile.TemporaryDirectory(prefix="crawler-chromium-") as profile,
    ):
        executable = args.executable or playwright.chromium.executable_path
        command = [
            executable,
            "--headless=new",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "about:blank",
        ]
        with subprocess.Popen(command) as process:
            print(f"Chromium sidecar: http://127.0.0.1:{args.port} (Ctrl+C to stop)", flush=True)
            try:
                process.wait()
            except KeyboardInterrupt:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
