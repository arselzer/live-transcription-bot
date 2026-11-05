#!/usr/bin/env python3
"""Start a persistent configured Whisper engine. CPU services never use GPU layers."""

import argparse
import os
import fcntl
from pathlib import Path
from urllib.parse import urlsplit
from live_transcript.core.config import get


def command(device):
    cpu = device != "gpu"
    url = (
        get("asr", "small_cpu_url" if device == "cpu-small" else "cpu_url")
        if cpu
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", choices=("gpu", "cpu-large", "cpu-small"), required=True)
    a = ap.parse_args()
    state = Path(get("paths", "state"))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = os.open(state / ("asr-" + a.device + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lease, True)
    os.environ["HIP_VISIBLE_DEVICES"] = get("asr", "gpu_device") if a.device == "gpu" else ""
    argv = command(a.device)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
