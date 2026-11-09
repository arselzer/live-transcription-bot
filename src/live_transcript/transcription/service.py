#!/usr/bin/env python3
"""Start a persistent configured Whisper engine. CPU services never use GPU layers."""

import argparse
import os
import fcntl
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from live_transcript.core.config import get
from live_transcript.core.files import atomic_json


def endpoints():
    result = [("primary", os.environ.get("CAPTION_STT_URL", get("caption", "stt_url")), "gpu")]
    if get("asr", "cpu_fallback"):
        result.append(("cpu-large", get("asr", "cpu_url"), "cpu-large"))
        if get("asr", "adaptive_model"):
            result.append(("cpu-small", get("asr", "small_cpu_url"), "cpu-small"))
    return result


def identity(path):
    p = Path(path)
    info = p.stat()
    return dict(path=str(p.resolve()), bytes=info.st_size, mtime_ns=info.st_mtime_ns)


def process_ticks(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def owned_engine(state, device, supervisor):
    record = json.loads((Path(state) / f"asr-{device}.json").read_text())
    fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(")", 1)[1].split()
    if (
        record["supervisor_pid"] != supervisor
        or record["supervisor_ticks"] != process_ticks(supervisor)
        or int(fields[1]) != supervisor
        or fields[19] != record["ticks"]
    ):
        raise ValueError("Engine is not owned by the selected supervisor")
    return record


def owns_listener(pid, port):
    try:
        inodes = set()
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(descriptor)
                if target.startswith("socket:["):
                    inodes.add(target[8:-1])
            except OSError:
                continue
        for name in ("tcp", "tcp6"):
            for line in Path(f"/proc/{pid}/net/{name}").read_text().splitlines()[1:]:
                row = line.split()
                if (
                    row[3] == "0A"
                    and int(row[1].rsplit(":", 1)[1], 16) == port
                    and row[9] in inodes
                ):
                    return True
    except (OSError, ValueError, IndexError):
        pass
    return False


def probe(url, device, timeout=3):
    """HTTP readiness plus local managed provenance; never perform inference."""
    result = dict(
        ready=False, verified=False, device="unknown", detail="Speech endpoint unavailable"
    )
    try:
        with urllib.request.urlopen(url.rsplit("/", 1)[0] + "/health", timeout=timeout) as response:
            data = json.loads(response.read(8192))
            if not isinstance(data, dict):
                raise ValueError("Invalid speech health response")
            result["ready"] = response.status == 200 and data.get("status") == "ok"
        if not result["ready"]:
            result["detail"] = "Speech model is loading or unavailable"
            return result
        result["detail"] = "Endpoint ready; model and device are unverified"
        if urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            return result
        receipt = Path(get("paths", "state")) / f"asr-{device}.json"
        if not receipt.exists():
            return result  # external engines need not use our supervisor
        if receipt.stat().st_size > 65536:
            raise ValueError("Invalid managed engine receipt")
        record = json.loads(receipt.read_text())
        if not isinstance(record, dict):
            raise ValueError("Invalid managed engine receipt")
        if record.get("endpoint") != url:
            return result
        argv = command(device)
        pid = record["pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError("Invalid managed engine PID")
        actual = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        expected_device = "cpu" if "--no-gpu" in argv else "gpu"
        if (
            str(record["ticks"]) != process_ticks(pid)
            or actual != [os.fsencode(a) for a in argv]
            or record["model"] != identity(argv[argv.index("-m") + 1])
            or record.get("device") != expected_device
            or record.get("exited")
            or not owns_listener(pid, urlsplit(url).port or 80)
        ):
            raise ValueError("Managed model/device or process does not match the profile")
        result.update(
            verified=True,
            device=expected_device,
            detail=f"Managed {expected_device.upper()} model ready",
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        result.update(ready=False, detail="Speech readiness failed: " + type(exc).__name__)
    return result


def readiness():
    result = {}
    units = {"primary": "primary", "cpu-large": "cpu_large", "cpu-small": "cpu_small"}
    for role, url, device in endpoints():
        row = probe(url, device)
        unit = get("services", units[role])
        if not row["ready"] and unit and get("runtime", "supervisor") == "systemd":
            try:
                status = subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "show",
                        unit,
                        "--property=ConditionResult",
                        "--property=ActiveState",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                fields = dict(
                    line.split("=", 1) for line in status.stdout.splitlines() if "=" in line
                )
                if fields.get("ConditionResult") == "no" and fields.get("ActiveState") in (
                    "inactive",
                    "failed",
                ):
                    row.update(
                        held=True,
                        detail="Speech service is held by systemd conditions; source recording is independent",
                    )
            except (OSError, subprocess.SubprocessError):
                pass
        result[role] = row
    return result


def command(device):
    cpu = device != "gpu" or get("asr", "primary_device") == "cpu"
    url = (
        get("asr", "small_cpu_url" if device == "cpu-small" else "cpu_url")
        if device != "gpu"
        else get("caption", "stt_url")
    )
    endpoint = urlsplit(url)
    if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("managed ASR must listen on a local HTTP endpoint")
    binary = get("paths", "whisper_server")
    model = get("paths", "small_model" if device == "cpu-small" else "model")
    argv = [
        binary,
        "-m",
        model,
        "--vad",
        "-vm",
        get("paths", "vad"),
        "-l",
        "auto",
        "--host",
        endpoint.hostname,
        "--port",
        str(endpoint.port or 80),
        "--inference-path",
        endpoint.path or "/inference",
        "-t",
        str(get("asr", "cpu_threads") if cpu else 8),
    ]
    if cpu:
        argv.append("--no-gpu")
    return argv


def engine(device, parent):
    # Install parent-death behavior in a fresh interpreter, never preexec_fn in
    # a threaded supervisor. A killed controller cannot leave a model orphan.
    import ctypes

    if os.getppid() != parent:
        raise RuntimeError("ASR supervisor disappeared before engine startup")
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM) != 0:
        raise OSError("Unable to bind engine lifetime to its supervisor")
    if os.getppid() != parent:
        raise RuntimeError("ASR supervisor disappeared during engine startup")
    argv = command(device)
    os.execv(argv[0], argv)


def supervise(device, lease):
    """Retain bounded startup proof while forwarding engine logs to the caller."""
    argv = command(device)
    state = Path(get("paths", "state"))
    receipt = state / f"asr-{device}.json"
    startup = state / f"asr-{device}-startup.log"
    model = identity(argv[argv.index("-m") + 1])
    from live_transcript.worker import command as worker_command

    descriptor = os.open(startup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    proc = subprocess.Popen(
        worker_command("asr", "--device", device, "--engine-parent", str(os.getpid())),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        pass_fds=(lease,),
    )
    record = dict(
        schema=1,
        supervisor_pid=os.getpid(),
        supervisor_ticks=process_ticks(os.getpid()),
        pid=proc.pid,
        ticks=process_ticks(proc.pid),
        endpoint=(
            get("caption", "stt_url")
            if device == "gpu"
            else get("asr", "small_cpu_url" if device == "cpu-small" else "cpu_url")
        ),
        model=model,
        device="cpu" if "--no-gpu" in argv else "unverified",
        started=time.time(),
    )
    atomic_json(receipt, record)
    stopping = [None]

    def stop(signum, frame):
        if stopping[0] is None:
            stopping[0] = time.monotonic()
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}

    def logs():
        with os.fdopen(descriptor, "wb") as evidence:
            remaining = 256 * 1024
            while chunk := proc.stdout.readline(16384):
                if remaining:
                    try:
                        evidence.write(chunk[:remaining])
                        evidence.flush()
                        remaining -= min(len(chunk), remaining)
                    except OSError:
                        remaining = 0  # still drain the engine if startup evidence storage fails
                if (
                    b"whisper_backend_init_gpu: found GPU device" in chunk
                    and "--no-gpu" not in argv
                ):
                    record["device"] = "gpu"
                    try:
                        atomic_json(receipt, record)
                    except OSError:
                        pass  # readiness remains unverified; keep draining logs
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    pass  # log consumer loss must not terminate recognition

    reader = threading.Thread(target=logs, daemon=True)
    reader.start()
    try:
        while True:
            try:
                code = proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if stopping[0] is not None and time.monotonic() - stopping[0] > 10:
                    proc.kill()
        reader.join(timeout=2)
        record.update(exited=True, exit_code=code, ended=time.time())
        atomic_json(receipt, record)
        return code if code >= 0 else 128 - code
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", choices=("gpu", "cpu-large", "cpu-small"), required=True)
    ap.add_argument("--engine-parent", type=int, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.engine_parent is not None:
        engine(a.device, a.engine_parent)
        return
    state = Path(get("paths", "state"))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = os.open(
        state / ("asr-" + a.device + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.environ["HIP_VISIBLE_DEVICES"] = (
        get("asr", "gpu_device")
        if a.device == "gpu" and get("asr", "primary_device") == "gpu"
        else ""
    )
    try:
        argv = command(a.device)
        host = argv[argv.index("--host") + 1]
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        # Refuse an occupied endpoint before loading another multi-GB model.
        with socket.socket(family, socket.SOCK_STREAM) as available:
            available.bind((host, int(argv[argv.index("--port") + 1])))
        return supervise(a.device, lease)
    finally:
        os.close(lease)


if __name__ == "__main__":
    raise SystemExit(main())
