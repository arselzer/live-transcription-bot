"""Owned headed browser lifecycle for Google Meet.

CDP mode starts a normal browser with a private profile, then attaches through
loopback. The process group belongs to this context and is always reaped. It
never attaches to or terminates a pre-existing browser.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from live_transcript.core.config import get


@contextmanager
def google_context(playwright, profile, *, env, args, port=9222):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid owned browser control port")
    endpoint = f"http://127.0.0.1:{port}"
    args = [arg for arg in args if not arg.startswith("--remote-debugging-")]
    args += [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
    executable = get("browser", "google_executable") or playwright.chromium.executable_path
    if get("browser", "google_mode") == "playwright":
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            executable_path=executable,
            headless=False,
            env=env,
            ignore_default_args=["--mute-audio"],
            locale="en-US",
            args=args,
            viewport={"width": 1280, "height": 800},
        )
        try:
            yield context
        finally:
            context.close()
        return

    # A listening endpoint could belong to an unrelated browser. Fail closed.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            raise RuntimeError("Google browser CDP port is already owned")
    except OSError:
        pass
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = Path(get("vm", "home")) / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        logs / f"google-browser-{time.time_ns()}.log",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w") as log:
        process = subprocess.Popen(
            [
                executable,
                "--user-data-dir=" + str(profile),
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=en-US",
                *args,
                "about:blank",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        "Google browser exited before its control endpoint was ready"
                    )
                try:
                    with urllib.request.urlopen(endpoint + "/json/version", timeout=0.3):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise TimeoutError("Google browser control endpoint did not become ready")
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=5000)
            if not browser.contexts:
                raise RuntimeError("Google browser has no owned profile context")
            context = (
                browser.new_context(locale="en-US", viewport={"width": 1280, "height": 800})
                if get("browser", "google_private")
                else browser.contexts[0]
            )
            yield context
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
