"""Durable application jobs for containers without a host service manager.

Only named application workers can be submitted. SQLite commits before launch;
each job wrapper owns a process group and records its exit independently of the
scheduler. Container replacement resumes retryable work, never a meeting join.
The guest recorder remains separately supervised inside its VM.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from live_transcript.core.config import get
from live_transcript.core.files import atomic_json

KINDS = {
    "launch",
    "mirror",
    "recover",
    "process",
    "console",
    "watchdog",
    "calendar",
    "calendar-dispatch",
}
ENV_KEYS = {"LT_EXPECT_RUN_ID", "LT_DEADLINE", "LT_WAIT_FOR_LOCK", "LT_CALENDAR_JOB"}
TERMINAL = {"complete", "failed", "interrupted", "expired", "cancelled"}


def identity():
    trust = get("vm", "known_hosts")
    trust_hash = hashlib.sha256(Path(trust).read_bytes()).hexdigest() if trust else ""
    values = [
        get("vm", "ssh"),
        get("vm", "name"),
        get("vm", "home"),
        get("paths", "root"),
        trust_hash,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def stamp(pid):
    try:
        values = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return values[19] if values[0] not in ("Z", "X") else None
    except (OSError, ValueError, IndexError):
        return None


def namespace():
    return (
        os.readlink("/proc/self/ns/pid")
        + ":"
        + Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    )


def alive(row, prefix=""):
    return bool(
        row.get(prefix + "pid")
        and row.get("namespace") == namespace()
        and stamp(row[prefix + "pid"]) == row.get(prefix + "ticks")
    )


def terminate(row, *, force=False):
    try:
        if force and alive(row, "child_") and os.getpgid(row["child_pid"]) == row["child_pid"]:
            os.killpg(row["child_pid"], signal.SIGKILL)
        if alive(row):
            os.kill(row["pid"], signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def directory():
    return Path(get("paths", "state")) / "application"


class Queue:
    def __init__(self, root=None):
        self.root = Path(root or directory())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / "jobs.sqlite3"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, due REAL, state TEXT)"
        )

    def close(self):
        self.db.close()

    def row(self, ident):
        row = self.db.execute("SELECT state FROM jobs WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, row):
        row["updated"] = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)",
            (row["id"], row["status"], row["due"], json.dumps(row, allow_nan=False)),
        )

    def submit(self, kind, arguments, *, key=None, due=None, env=None, period=0):
        arguments, env = list(arguments), dict(env or {})
        if (
            kind not in KINDS
            or len(arguments) > 40
            or any(not isinstance(x, str) or len(x) > 16384 or "\0" in x for x in arguments)
            or len(json.dumps(arguments).encode()) > 65536
            or set(env) - ENV_KEYS
            or any(not isinstance(v, str) or len(v) > 512 or "\0" in v for v in env.values())
        ):
            raise ValueError("Invalid application job")
        key = key or (kind + "-" + uuid.uuid4().hex)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", key):
            raise ValueError("Invalid application job identity")
        now = time.time()
        if due is not None and (
            type(due) not in (int, float) or not now - 3600 <= due <= now + 172800
        ):
            raise ValueError("Invalid application job time")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.row(key)
            if old:
                if (old["kind"], old["arguments"], old["env"], old["identity"]) != (
                    kind,
                    arguments,
                    env,
                    identity(),
                ):
                    raise ValueError("Application job identity belongs to different work")
                return old
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','starting')"
                ).fetchone()[0]
                >= 1024
            ):
                raise RuntimeError("Application job queue is full")
            if kind == "launch" and not env.get("LT_EXPECT_RUN_ID"):
                if self.db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running','starting') AND json_extract(state,'$.kind')='launch'"
                ).fetchone():
                    raise RuntimeError("A meeting launch is already owned by the application")
            row = dict(
                id=key,
                kind=kind,
                arguments=arguments,
                env=env,
                identity=identity(),
                status="queued",
                due=due or now,
                created=now,
                deadline=(due or now) + get("runtime", "job_timeout_seconds"),
                attempts=0,
                period=period,
                pid=None,
                ticks=None,
                namespace=None,
            )
            self.save(row)
        return row

    def active(self):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT state FROM jobs WHERE status IN ('queued','starting','running') ORDER BY due"
            )
        ]

    def finish(self, row, code, *, interrupted=False, abandoned=False):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            latest = self.row(row["id"])
            if not latest or latest.get("nonce") != row.get("nonce"):
                return
            if abandoned and alive(latest):
                return
            row = latest
            retry = (
                row["kind"] in ("mirror", "recover", "process", "calendar-dispatch")
                and row["attempts"] < get("runtime", "job_attempts")
                and time.time() < row["deadline"]
            )
            row.update(
                status="complete" if code == 0 else "interrupted" if interrupted else "failed",
                returncode=code,
            )
            if row.get("period"):
                row.update(
                    status="queued",
                    due=time.time() + row["period"],
                    attempts=0,
                    deadline=time.time() + get("runtime", "job_timeout_seconds"),
                )
            elif code and retry:
                row.update(status="queued", due=time.time() + min(60, 5 * 2 ** row["attempts"]))
            self.save(row)


def ready():
    try:
        value = json.loads((directory() / "health.json").read_text())
        return 0 <= time.time() - value["utc"] < 10 and alive(value)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def submit(kind, arguments, **options):
    if not ready():
        raise RuntimeError(
            "Application supervisor is unavailable; start 'live-transcript serve app'"
        )
    queue = Queue()
    try:
        return queue.submit(kind, arguments, **options)
    finally:
        queue.close()


def command(row):
    from live_transcript.worker import command as worker

    if row["kind"] == "launch":
        from live_transcript.runtime.launcher import command as launch

        return launch(*row["arguments"])
    if row["kind"] == "console":
        return [
            sys.executable,
            "-u",
            "-m",
            "live_transcript",
            "--json",
            "console",
            *row["arguments"],
        ]
    if row["kind"] == "recover":
        if len(row["arguments"]) != 3 or row["arguments"][0] != get("vm", "ssh"):
            raise ValueError("Recovery job does not match the configured VM")
        # The public CLI maps failed retrieval/processing checkpoints to a
        # nonzero exit. The legacy internal worker doesn't report that result.
        return [
            sys.executable,
            "-u",
            "-m",
            "live_transcript",
            "--json",
            "recover",
            row["arguments"][2],
            row["arguments"][1],
        ]
    return worker(row["kind"], *row["arguments"])


def job(ident, nonce):
    queue = Queue()
    try:
        with queue.db:
            queue.db.execute("BEGIN IMMEDIATE")
            row = queue.row(ident)
            if (
                not row
                or row.get("nonce") != nonce
                or row["status"] != "starting"
                or row["identity"] != identity()
            ):
                raise RuntimeError("Application job claim changed")
            row.update(
                status="running", pid=os.getpid(), ticks=stamp(os.getpid()), namespace=namespace()
            )
            queue.save(row)
    except BaseException:
        queue.close()
        raise
    from live_transcript.runtime.launcher import environment

    stopped = False
    proc = None

    def stop(*_):
        nonlocal stopped
        stopped = True
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        env = environment(dict(os.environ, **row["env"], LT_APPLICATION_JOB=ident))
        proc = subprocess.Popen(
            command(row), env=env, stdin=subprocess.DEVNULL, start_new_session=True
        )
        row.update(child_pid=proc.pid, child_ticks=stamp(proc.pid))
        with queue.db:
            queue.save(row)
        while proc.poll() is None:
            if stopped:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                break
            time.sleep(0.2)
        code = proc.wait()
        queue.finish(row, code, interrupted=stopped)
        return code
    finally:
        queue.close()


def cancel(key):
    queue = Queue()
    try:
        with queue.db:
            row = queue.row(key)
            if row and row["status"] == "queued":
                row["status"] = "cancelled"
                queue.save(row)
    finally:
        queue.close()


def status(key):
    queue = Queue()
    try:
        return queue.row(key)
    finally:
        queue.close()


def follow(row, *, announce=False):
    if announce:
        print("Application job:", row["id"], flush=True)
    position = 0
    while True:
        try:
            with (directory() / (row["id"] + ".log")).open("rb") as stream:
                stream.seek(position)
                data = stream.read()
                position = stream.tell()
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        except FileNotFoundError:
            pass
        current = status(row["id"])
        if current["status"] in TERMINAL:
            try:
                with (directory() / (row["id"] + ".log")).open("rb") as stream:
                    stream.seek(position)
                    sys.stdout.buffer.write(stream.read())
                    sys.stdout.buffer.flush()
            except FileNotFoundError:
                pass
            return current.get("returncode") or (0 if current["status"] == "complete" else 1)
        if not ready():
            raise RuntimeError(
                "Application supervisor unavailable; retained jobs resume when it returns"
            )
        time.sleep(0.5)


def serve():
    os.umask(0o077)
    queue = Queue()
    fd = os.open(queue.root / "supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    owned = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Application supervisor is already running") from exc
        owned = True
        stopping = False
        children = {}

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        atomic_json(
            queue.root / "health.json",
            dict(utc=time.time(), pid=os.getpid(), ticks=stamp(os.getpid()), namespace=namespace()),
        )
        for kind, seconds in (
            ("watchdog", get("runtime", "watchdog_seconds")),
            ("calendar", get("runtime", "calendar_seconds")),
        ):
            if seconds:
                row = queue.submit(kind, [], key="periodic-" + kind, period=seconds)
                row["period"] = seconds
                if row["status"] in TERMINAL:
                    row.update(status="queued", due=time.time())
                with queue.db:
                    queue.save(row)
            else:
                row = queue.row("periodic-" + kind)
                if row:
                    row["period"] = 0
                    if row["status"] == "queued":
                        row["status"] = "cancelled"
                    with queue.db:
                        queue.save(row)
        while not stopping:
            rows = queue.active()
            for row in rows:
                if row["identity"] != identity():
                    raise RuntimeError("Unfinished jobs belong to a different VM or data root")
                if row["status"] in ("starting", "running"):
                    if alive(row):
                        if time.time() > row["deadline"]:
                            terminate(row, force=time.time() > row["deadline"] + 20)
                        continue
                    if row["status"] == "starting" and time.time() - row["updated"] < 10:
                        continue
                    # A killed wrapper can leave its child behind on a native
                    # restart. Only signal the recorded process group identity.
                    if alive(row, "child_") and os.getpgid(row["child_pid"]) == row["child_pid"]:
                        os.killpg(row["child_pid"], signal.SIGTERM)
                        time.sleep(0.2)
                        if alive(row, "child_"):
                            os.killpg(row["child_pid"], signal.SIGKILL)
                    queue.finish(row, 1, interrupted=True, abandoned=True)
                    continue
                if row["due"] > time.time():
                    continue
                if sum(alive(r) or r["status"] == "starting" for r in rows) >= get(
                    "runtime", "max_jobs"
                ):
                    continue
                if row["kind"] in ("recover", "process") and any(
                    r["kind"] in ("recover", "process") and (alive(r) or r["status"] == "starting")
                    for r in rows
                ):
                    continue  # final recognition is serialized to preserve capacity
                if row["deadline"] < time.time():
                    row["status"] = "expired"
                    with queue.db:
                        queue.save(row)
                    continue
                row.update(
                    status="starting",
                    nonce=uuid.uuid4().hex,
                    attempts=row["attempts"] + 1,
                    pid=None,
                    ticks=None,
                )
                with queue.db:
                    queue.save(row)
                log = queue.root / (row["id"] + ".log")
                with log.open("ab") as stream:
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            "-u",
                            "-m",
                            "live_transcript.worker",
                            "application-job",
                            row["id"],
                            row["nonce"],
                        ],
                        stdout=stream,
                        stderr=stream,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                children[proc.pid] = proc
            for pid, proc in list(children.items()):
                if proc.poll() is not None:
                    del children[pid]
            atomic_json(
                queue.root / "health.json",
                dict(
                    utc=time.time(),
                    pid=os.getpid(),
                    ticks=stamp(os.getpid()),
                    namespace=namespace(),
                ),
            )
            time.sleep(0.5)
        deadline = time.monotonic() + 20
        signalled = set()
        while time.monotonic() < deadline:
            active = queue.active()
            for row in active:
                if alive(row) and (row["id"], row["pid"]) not in signalled:
                    terminate(row)
                    signalled.add((row["id"], row["pid"]))
            if not any(alive(row) or row["status"] == "starting" for row in active):
                break
            for proc in children.values():
                proc.poll()
            time.sleep(0.2)
        for row in queue.active():
            terminate(row, force=True)
    finally:
        if owned:
            (queue.root / "health.json").unlink(missing_ok=True)
        queue.close()
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--submit", choices=sorted(KINDS))
    parser.add_argument("--key")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.health:
        return 0 if ready() else 1
    if args.submit:
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        print(json.dumps(dict(job=submit(args.submit, arguments, key=args.key)["id"])), flush=True)
        return 0
    return serve()


if __name__ == "__main__":
    if len(sys.argv) == 3 and not sys.argv[1].startswith("--"):
        raise SystemExit(job(*sys.argv[1:]))
    raise SystemExit(main())
