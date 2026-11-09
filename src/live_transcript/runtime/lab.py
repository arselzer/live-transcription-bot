"""Private, serialized Jitsi lab runs and reference-backed regression reports."""

from array import array
from contextlib import ExitStack
from copy import deepcopy
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import signal
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import threading
import urllib.request
import time
import tomllib
from urllib.parse import urlsplit
import uuid
import wave

from live_transcript.core import config
from live_transcript.core.config import get, ssh_options
from live_transcript.core.files import atomic_json, atomic_text
from live_transcript.transcription import evaluation
from live_transcript.worker import command, command_env

PHRASES = (
    ("en", "The copper lantern is beside the window."),
    ("en", "Please deliver seven green notebooks before Thursday."),
    ("en", "We agreed to review the final report tomorrow."),
    ("de", "Die blaue Tasse steht neben dem Fenster."),
    ("de", "Bitte bringt sieben grüne Hefte bis Donnerstag."),
    ("de", "Wir besprechen den letzten Bericht morgen gemeinsam."),
)
SCENARIOS = ("clean", "quiet", "noise", "overlap")


def write_profile(path, values):
    atomic_text(
        path,
        "\n\n".join(
            "["
            + section
            + "]\n"
            + "\n".join(k + " = " + json.dumps(v, ensure_ascii=False) for k, v in row.items())
            for section, row in values.items()
            if not section.startswith("_")
        )
        + "\n",
    )


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def server_url(value):
    from live_transcript.providers.jitsi import _configured_hosts

    p = urlsplit(value)
    if (
        p.scheme != "https"
        or not p.hostname
        or p.username is not None
        or p.password is not None
        or p.path not in ("", "/")
        or p.query
        or p.fragment
        or p.port == 0
        or p.hostname not in _configured_hosts()
        or p.hostname.endswith(".jit.si")
    ):
        raise ValueError("Use an explicitly allowlisted, self-owned HTTPS Jitsi server origin")
    return f"https://{p.netloc}"


def ssh():
    return [
        "ssh",
        *ssh_options(),
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-T",
        get("vm", "ssh"),
    ]


def remote(arguments, *, data=None, timeout=20):
    return subprocess.run(
        [*ssh(), shlex.join(arguments)],
        input=data,
        capture_output=True,
        check=True,
        timeout=timeout,
    ).stdout


def upload(root, files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, content in files.items():
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Unsafe lab artifact name")
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(content), 0o600
            archive.addfile(item, io.BytesIO(content))
    # A new UUID directory is created atomically before unpacking trusted input.
    program = "import pathlib,sys,tarfile; p=pathlib.Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=False,mode=0o700); a=tarfile.open(fileobj=sys.stdin.buffer,mode='r|'); a.extractall(p,filter='data')"
    remote([get("vm", "python"), "-c", program, str(root)], data=data.getvalue(), timeout=40)


def preflight_asr(endpoint=None):
    endpoints = [endpoint or get("caption", "stt_url")]
    if get("asr", "cpu_fallback"):
        endpoints.extend([get("asr", "cpu_url"), get("asr", "small_cpu_url")])
    results = []
    for url in dict.fromkeys(endpoints):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("Use a plain HTTP(S) inference endpoint")
        try:
            with urllib.request.urlopen(url.rsplit("/", 1)[0] + "/health", timeout=3) as response:
                results.append(dict(endpoint=url, ready=response.status == 200))
        except (OSError, ValueError):
            results.append(dict(endpoint=url, ready=False))
    return dict(ok=any(r["ready"] for r in results), endpoints=results)


def synthesize(directory, plan=None):
    directory.mkdir(mode=0o700)
    rows = []
    if plan:
        return import_plan(Path(plan), directory)
    for i, (language, text) in enumerate(PHRASES):
        with socket.create_connection(
            (get("caption", "host"), get("caption", "port")), timeout=120
        ) as connection:
            connection.sendall(f"SAY {language} {text}\n".encode())
            connection.shutdown(socket.SHUT_WR)
            with connection.makefile("rb") as stream:
                source = stream.read(8 * 1024 * 1024 + 1)
        if not 44 < len(source) <= 8 * 1024 * 1024:
            raise RuntimeError("The configured synthesizer did not return bounded WAV audio")
        raw = directory / f"source-{i}.wav"
        raw.write_bytes(source)
        target = directory / f"phrase-{i}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(raw),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-n",
                str(target),
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        with wave.open(str(target)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        if not 0.2 <= duration <= 20:
            raise ValueError("Synthetic phrase duration is outside the lab budget")
        rows.append(
            dict(
                id=f"phrase-{i}",
                language=language,
                text=text,
                file=target.name,
                duration=duration,
                speaker="Lab Speaker A" if language == "en" else "Lab Speaker B",
            )
        )
    return rows


def import_plan(path, directory):
    raw = json.loads(path.read_text())
    raw = raw.get("phrases") if isinstance(raw, dict) else raw
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError("Custom plan needs 1–100 phrases")
    rows = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("Invalid custom phrase")
        source = (path.parent / item["file"]).resolve()
        if (
            not source.is_relative_to(path.parent.resolve())
            or not source.is_file()
            or source.stat().st_size > 8 * 1024**2
        ):
            raise ValueError("Custom phrase audio must stay beneath its plan directory")
        with wave.open(str(source)) as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError("Custom phrases require 16 kHz mono PCM16 WAV")
            duration = audio.getnframes() / audio.getframerate()
        row = dict(item, id=str(item["id"]), duration=duration, file=f"phrase-{i}.wav")
        evaluation.validate_reference([dict(row, start=0, end=duration)])
        (directory / row["file"]).write_bytes(source.read_bytes())
        if sum(r["duration"] + 3 for r in rows) + duration + 3 > 900:
            raise ValueError("Custom plan exceeds its duration budget")
        rows.append(row)
    if (
        len({r["id"] for r in rows}) != len(rows)
        or len({r["speaker"] for r in rows}) > 3
        or sum(r["duration"] + 3 for r in rows) > 900
        or sum(len(evaluation.words(r["text"])) for r in rows) > 1500
    ):
        raise ValueError("Custom plan has duplicate IDs or exceeds its speaker/duration budget")
    return rows


def prepare_inputs(source, destination, phrases, scenario):
    destination.mkdir(mode=0o700)
    rows, offset = [], 0
    speaker_offsets = {}
    for i, phrase in enumerate(phrases):
        with wave.open(str(source / phrase["file"])) as audio:
            pcm = array("h", audio.readframes(audio.getnframes()))
        if scenario == "quiet":
            pcm = array("h", (round(v * 10 ** (-24 / 20)) for v in pcm))
        elif scenario == "noise":
            rng = random.Random(i)
            rms = math.sqrt(sum(v * v for v in pcm) / max(1, len(pcm)))
            pcm = array(
                "h",
                (
                    max(-32768, min(32767, round(v + rng.gauss(0, rms / math.sqrt(10)))))
                    for v in pcm
                ),
            )
        target = destination / phrase["file"]
        with wave.open(str(target), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(pcm.tobytes())
        if scenario == "overlap":
            offset = speaker_offsets.setdefault(phrase["speaker"], len(speaker_offsets) * 1.0)
        if "offset" in phrase:
            offset = phrase["offset"]
            if type(offset) not in (int, float) or not math.isfinite(offset) or offset < 0:
                raise ValueError("Invalid custom phrase offset")
        rows.append(
            dict(
                phrase,
                offset=offset,
                start=offset,
                end=offset + phrase["duration"],
                condition=scenario,
                audio_sha256=digest(target),
            )
        )
        offset += phrase["duration"] + 2.5
        speaker_offsets[phrase["speaker"]] = offset
    evaluation.validate_reference(rows)
    if max(r["end"] for r in rows) > 900:
        raise ValueError("Lab schedule exceeds 15 minutes")
    last_end = {}
    for row in sorted(rows, key=lambda r: r["start"]):
        if row["start"] < last_end.get(row["speaker"], 0):
            raise ValueError("A scripted speaker cannot play overlapping phrases with itself")
        last_end[row["speaker"]] = row["end"]
    return rows


class CaptionObserver:
    def __init__(self, host, port, session, destination):
        self.host, self.port, self.session, self.destination = host, port, session, destination
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with self.destination.open("a", buffering=1) as output:
            while not self.stop.is_set():
                try:
                    with socket.create_connection((self.host, self.port), timeout=2) as connection:
                        connection.settimeout(2)
                        connection.sendall(f"SUBCAP {self.session}\n".encode())
                        with connection.makefile("rb") as stream:
                            for raw in stream:
                                if self.stop.is_set():
                                    break
                                if len(raw) > 1024 * 1024:
                                    raise ValueError("Oversized caption snapshot")
                                output.write(
                                    json.dumps(
                                        dict(received_at=time.time(), payload=json.loads(raw))
                                    )
                                    + "\n"
                                )
                except (OSError, ValueError):
                    self.stop.wait(0.2)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=4)


def profile_environment(profile):
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CAPTION_", "LT_", "LIVE_TRANSCRIPT_"))
    }
    environment["LIVE_TRANSCRIPT_CONFIG"] = str(profile)
    return command_env(sys.executable, base=environment) or environment


def owns_listener(pid, port):
    """A readiness response must come from our child, not a raced listener."""
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


def guarded_command(arguments):
    from live_transcript.runtime.application import stamp

    return command("lab-local", str(os.getpid()), stamp(os.getpid()), *arguments)


def guarded_child():
    """Set parent-death behavior in a fresh process, never a threaded fork hook."""
    import ctypes
    from live_transcript.runtime.application import stamp

    parent, ticks = int(sys.argv[1]), sys.argv[2]
    arguments = sys.argv[3:]
    if not arguments or arguments[0] not in (sys.executable, "ssh"):
        raise ValueError("Invalid local lab worker")
    if not sys.platform.startswith("linux") or ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0):
        raise RuntimeError("Lab worker parent-death protection is unavailable")
    if os.getppid() != parent or stamp(parent) != ticks:
        raise RuntimeError("Lab controller exited before its worker started")
    os.execvp(arguments[0], arguments)


def start_caption(profile, host, port, log, token=""):
    environment = profile_environment(profile)
    environment["CAPTION_OWNER_TOKEN"] = token
    with log.open("ab") as stream:
        process = subprocess.Popen(
            guarded_command(command("captions")),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    end = time.monotonic() + 25
    while process.poll() is None and time.monotonic() < end:
        try:
            if owns_listener(process.pid, port):
                return process
        except OSError:
            time.sleep(0.2)
    from live_transcript.runtime.lab_guest import stop_process

    stop_process(process)
    raise RuntimeError("Isolated caption service did not start; inspect caption.log")


def start_tunnel(local_port, guest_port, token, log):
    from live_transcript.runtime.lab_guest import stop_process

    base = ssh()
    with log.open("ab") as stream:
        process = subprocess.Popen(
            guarded_command(
                [
                    *base[:-1],
                    "-N",
                    "-o",
                    "ControlMaster=no",
                    "-o",
                    "ControlPath=none",
                    "-o",
                    "ForkAfterAuthentication=no",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-R",
                    f"127.0.0.1:{guest_port}:127.0.0.1:{local_port}",
                    base[-1],
                ]
            ),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    probe = "import socket,json,sys; s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=2); s.sendall(b'GETGPU\\n'); print(s.makefile('rb').readline(65536).decode())"
    until = time.monotonic() + 20
    while process.poll() is None and time.monotonic() < until:
        try:
            value = json.loads(
                remote([get("vm", "python"), "-c", probe, str(guest_port)], timeout=5)
            )
            if (
                isinstance(value, dict)
                and value.get("server_nonce") == token
                and process.poll() is None
            ):
                return process
        except (ValueError, OSError, subprocess.SubprocessError):
            time.sleep(0.2)
    stop_process(process)
    raise RuntimeError("Owned SSH caption tunnel did not become ready; inspect tunnel.log")


def start_engine(profile, endpoint, log):
    from live_transcript.runtime.lab_guest import stop_process

    environment = profile_environment(profile)
    started = time.monotonic()
    with log.open("ab") as stream:
        log_offset = stream.tell()
        process = subprocess.Popen(
            guarded_command(command("asr", "--device", "gpu")),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    while process.poll() is None and time.monotonic() - started < 120:
        try:
            with urllib.request.urlopen(
                endpoint.rsplit("/", 1)[0] + "/health", timeout=1
            ) as response:
                if response.status == 200 and owns_listener(process.pid, urlsplit(endpoint).port):
                    selected = tomllib.loads(profile.read_text())["asr"]["primary_device"]
                    with log.open("rb") as startup:
                        startup.seek(log_offset)
                        accelerator = b"whisper_backend_init_gpu: found GPU device" in startup.read(
                            256 * 1024
                        )
                    if selected == "gpu" and not accelerator:
                        stop_process(process)
                        raise RuntimeError(
                            "GPU lab mode could not verify an accelerator; use a GPU-enabled whisper-server or select CPU mode"
                        )
                    atomic_json(
                        log.with_suffix(".json"),
                        dict(model_ready_seconds=time.monotonic() - started),
                    )
                    return process
        except OSError:
            time.sleep(0.25)
    stop_process(process)
    raise RuntimeError("Owned full-model inference server did not become ready")


def run_owned(arguments, environment, log, timeout):
    """Bound final recognition's whole process group, including decoder children."""
    from live_transcript.runtime.lab_guest import stop_process

    with log.open("ab") as stream:
        process = subprocess.Popen(
            guarded_command(arguments),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    try:
        code = process.wait(timeout=timeout)
    except BaseException:
        stop_process(process)
        raise
    if code:
        raise subprocess.CalledProcessError(code, arguments)


def collect(case, receipt):
    if receipt["identity"] != installation_identity():
        raise ValueError("Lab receipt belongs to a different VM or installation")
    root = receipt["remote"]
    expected = Path(get("vm", "home")) / ".local/share/live-transcript-lab" / receipt["token"]
    if root != str(expected) or not re.fullmatch(r"[a-f0-9]{32}", receipt["token"]):
        raise ValueError("Invalid retained lab receipt")
    target = case / "guest"
    target.mkdir(exist_ok=True, mode=0o700)
    subprocess.run(
        [
            "rsync",
            "-a",
            "--no-links",
            "--protect-args",
            "--include=/rec/***",
            "--include=/*.json",
            "--include=/*.jsonl",
            "--include=/*.log",
            "--exclude=*",
            "-e",
            shlex.join(["ssh", *ssh_options(), "-o", "ConnectTimeout=5"]),
            get("vm", "ssh") + ":" + root + "/",
            str(target) + "/",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return target


def installation_identity():
    from live_transcript.runtime.application import identity

    return identity()


def canonical_audio(case, archive, session):
    """Score a verified archive reconstruction; keep the convenience WAV intact."""
    from live_transcript.storage.recover import recover

    directory = case / "analysis"
    directory.mkdir(exist_ok=True, mode=0o700)
    output = directory / (session + ".wav")
    fingerprint = digest(archive / "integrity.json")
    marker = directory / "audio-source.json"
    if output.exists():
        previous = json.loads(marker.read_text())
        if previous["inventory_sha256"] != fingerprint or previous["audio_sha256"] != digest(
            output
        ):
            raise ValueError("Derived lab audio changed; existing evidence was retained")
        return output, json.loads(output.with_suffix(".recovery.json").read_text())
    original = archive.parent / (session + ".wav")
    monitor = output.with_suffix(".monitor.wav")
    if original.exists() and not monitor.exists():
        os.link(original, monitor)
    result = recover(archive, output)
    atomic_json(marker, dict(inventory_sha256=fingerprint, audio_sha256=digest(output)))
    return output, result


def assess(case):
    from live_transcript.transcription.store import CaptionStore

    receipt = json.loads((case / "receipt.json").read_text())
    guest = case / "guest"
    if not (guest / "result.json").exists():
        raise RuntimeError(
            "Guest result is not yet available; inspect its owned unit and retry collect without joining again"
        )
    result = json.loads((guest / "result.json").read_text())
    if not result.get("ok"):
        return dict(result, stage="guest", evidence_retained=True)
    session = receipt["session"]
    archive = guest / "rec" / (session + ".archive")
    from live_transcript.capture.guard import verify_inventory

    inventory = verify_inventory(archive)
    events = [json.loads(line) for line in (archive / "events.jsonl").read_text().splitlines()]
    origin = (
        json.loads((archive / "config.json").read_text())["started"]
        if (archive / "config.json").exists()
        else next(row["utc"] for row in events if row["kind"] == "primary_capture_started")
    )
    reference = []
    for path in sorted(guest.glob("sender-*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            reference.append(dict(row, start=row["started"] - origin, end=row["ended"] - origin))
    if len(reference) != len(receipt["phrases"]):
        raise RuntimeError("Scripted playback was incomplete; evidence retained, no passing score")
    reference = evaluation.validate_reference(reference)
    atomic_json(case / "reference.json", reference)
    wav, reconstruction = canonical_audio(case, archive, session)
    store = CaptionStore(str(case / "runtime/state/captions.sqlite3"))
    try:
        lines = [
            dict(t=t, end=t, speaker=speaker, text=text)
            for t, speaker, text in store.transcript(session)
        ]
    finally:
        store.close()
    atomic_json(case / "live-transcript.json", lines)
    captions = []
    for raw in (case / "captions.jsonl").read_text().splitlines():
        event = json.loads(raw)
        captions.append(
            dict(
                received_t=event["received_at"] - receipt["clock_offset"] - origin,
                lines=[
                    dict(t=r[0], speaker=r[1], text=r[2]) for r in event["payload"].get("lines", [])
                ],
            )
        )
    measured = evaluation.score(reference, lines, caption_events=captions)
    decode = []
    queued = []
    engines = set()
    for raw in (case / "captions.jsonl").read_text().splitlines():
        payload = json.loads(raw)["payload"]
        value = payload.get("decode_ms")
        if type(value) in (int, float) and value > 0:
            decode.append(value)
        queued.append(payload.get("queued_audio_s", 0))
        engine = payload.get("asr", {}).get("engine")
        if engine:
            engines.add(engine)
    capture_ok = (
        result.get("ok") is True
        and result.get("finalized") is True
        and (archive / "finalized.json").is_file()
    )
    recognized = measured["tokens"]["recognized_tokens"] > 0
    missing_capture = any(
        g["seconds"] - g.get("backup_seconds", 0) > 0.05
        and any(g["start"] < r["end"] and r["start"] < g["start"] + g["seconds"] for r in reference)
        for g in reconstruction.get("gaps", [])
    )
    return dict(
        capture_ok=capture_ok,
        error=(
            "Capture gap intersects reference speech"
            if missing_capture
            else None
            if recognized
            else "No live reference speech was recognized"
        ),
        reconstruction=reconstruction,
        provenance={
            key: receipt.get(key)
            for key in (
                "bundle_sha256",
                "profile_sha256",
                "package_version",
                "model_identity",
                "asr_mode",
                "silence_finalize",
            )
        },
        ok=capture_ok and recognized and not missing_capture,
        live=measured,
        performance=dict(
            published_decode_ms_p95=evaluation.percentile(decode, 0.95),
            max_queued_audio_seconds=max(queued, default=0),
            engines=sorted(engines),
            caption_snapshots=len(captions),
        ),
        archive_files_verified=inventory,
        timing=dict(
            reference="sender playback / primary capture start (UTC estimates)",
            host_guest_clock_uncertainty_seconds=receipt["clock_uncertainty"],
        ),
        recording=str(wav.relative_to(case)),
    )


def drain_caption(host, port, session, destination):
    samples, settled = [], 0
    until = time.monotonic() + 35
    while time.monotonic() < until:
        try:
            with socket.create_connection((host, port), timeout=2) as connection:
                connection.sendall(f"GETGPU {session}\n".encode())
                with connection.makefile("rb") as stream:
                    value = json.loads(stream.readline(65536))
            samples.append(dict(utc=time.time(), **value))
            settled = (
                settled + 1
                if (
                    not value.get("decode_busy")
                    and not value.get("finalize_pending")
                    and value.get("chunk_age", -1) >= 2
                )
                else 0
            )
            if settled >= 3:
                atomic_json(destination, dict(settled=True, samples=samples))
                return
        except (OSError, ValueError):
            settled = 0
        time.sleep(1)
    atomic_json(destination, dict(settled=False, samples=samples))


def run_case(
    directory,
    server,
    scenario,
    phrases,
    source,
    *,
    endpoint=None,
    final=True,
    asr_mode="existing",
    silence_finalize=None,
):
    from live_transcript.runtime.deployment import build_bundle
    from live_transcript.runtime.lab_guest import stop_process

    case = directory / scenario
    case.mkdir(mode=0o700)
    rendered = prepare_inputs(source, case / "inputs", phrases, scenario)
    token = uuid.uuid4().hex
    session = "lab-" + token
    root = Path(get("vm", "home")) / ".local/share/live-transcript-lab" / token
    url = server + "/LT-Lab-" + token
    host = "127.0.0.1"
    with socket.socket() as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    before = time.time()
    data = json.loads(
        remote(
            [
                get("vm", "python"),
                "-c",
                "import socket,time,json; s=socket.socket(); s.bind(('127.0.0.1',0)); c=socket.socket(); c.bind(('127.0.0.1',0)); print(json.dumps(dict(utc=time.time(),port=s.getsockname()[1],caption_port=c.getsockname()[1])))",
            ]
        ).decode()
    )
    after = time.time()
    values = deepcopy(config.CONFIG)
    values["paths"].update(
        root=str(case / "runtime"),
        state=str(case / "runtime/state"),
        logs=str(case / "runtime/logs"),
        recordings=str(case / "runtime/recordings"),
        safety_copies=str(case / "runtime/safety"),
    )
    for name in ("state", "logs", "recordings", "safety_copies"):
        Path(values["paths"][name]).mkdir(parents=True, mode=0o700)
    values["caption"].update(bind=host, host=host, port=port)
    if silence_finalize is not None:
        values["caption"]["silence_finalize"] = silence_finalize
    values["model"]["enabled"] = False
    values["services"] = {k: "" for k in values["services"]}
    values["browser"]["jitsi_debug_port"] = data["port"]
    if endpoint:
        values["caption"]["stt_url"] = endpoint
    if asr_mode != "existing":
        with socket.socket() as engine_socket:
            engine_socket.bind(("127.0.0.1", 0))
            engine_port = engine_socket.getsockname()[1]
        values["caption"]["stt_url"] = f"http://127.0.0.1:{engine_port}/inference"
        values["asr"].update(primary_device=asr_mode, cpu_fallback=False, adaptive_model=False)
    profile = case / "profile.toml"
    write_profile(profile, values)
    guest = {
        k: deepcopy(values[k])
        for k in (
            "audio",
            "browser",
            "providers",
            "identity",
            "recovery",
            "admission",
            "google",
            "model",
            "asr",
            "caption",
        )
    }
    guest.update(paths=dict(root=str(root)), vm=dict(home=str(root), python=get("vm", "python")))
    guest["caption"].update(host="127.0.0.1", port=data["caption_port"])
    guest_profile = case / "guest.toml"
    write_profile(guest_profile, guest)
    duration = max(row["end"] for row in rendered)
    job = dict(
        url=url,
        session=session,
        owner_home=get("vm", "home"),
        duration=duration,
        speakers=list(dict.fromkeys(p["speaker"] for p in rendered)),
        phrases=rendered,
    )
    bundle = case / "live-transcript.pyz"
    build_bundle(bundle)
    files = {
        "live-transcript.pyz": bundle.read_bytes(),
        "config.toml": guest_profile.read_bytes(),
        "job.json": json.dumps(job).encode(),
    }
    files.update({"inputs/" + p.name: p.read_bytes() for p in (case / "inputs").iterdir()})
    from importlib.metadata import version

    model = Path(values["paths"]["model"])
    model_stat = model.stat()
    receipt = dict(
        token=token,
        session=session,
        remote=str(root),
        identity=installation_identity(),
        unit="live-transcript-lab-" + token,
        phrases=rendered,
        clock_offset=data["utc"] - (before + after) / 2,
        clock_uncertainty=(after - before) / 2,
        bundle_sha256=digest(bundle),
        package_version=version("live-transcription-bot"),
        profile_sha256=digest(profile),
        model_identity=dict(
            path=str(model),
            bytes=model_stat.st_size,
            mtime_ns=model_stat.st_mtime_ns,
            limitation="File metadata, not a model-content hash",
        ),
        asr_mode=asr_mode,
        silence_finalize=values["caption"]["silence_finalize"],
        scenario=scenario,
    )
    atomic_json(case / "receipt.json", receipt)
    caption, observer, submitted, cleanup_error = None, None, False, None
    engine = None
    tunnel = None
    try:
        if asr_mode != "existing":
            engine = start_engine(profile, values["caption"]["stt_url"], case / "engine.log")
        caption = start_caption(profile, host, port, case / "caption.log", token=token)
        tunnel = start_tunnel(port, data["caption_port"], token, case / "tunnel.log")
        observer = CaptionObserver(host, port, session, case / "captions.jsonl")
        observer.thread.start()
        upload(root, files)
        remote(
            [
                get("vm", "python"),
                "-c",
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); (p/'rec').mkdir(); (p/'logs').mkdir()",
                str(root),
            ]
        )
        env = [
            "--setenv=LIVE_TRANSCRIPT_CONFIG=" + str(root / "config.toml"),
            "--setenv=LIVE_TRANSCRIPT_BUNDLE=" + str(root / "live-transcript.pyz"),
            "--setenv=XDG_RUNTIME_DIR=/run/user/" + remote(["id", "-u"]).decode().strip(),
        ]
        remote(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--unit=" + receipt["unit"],
                "--property=RuntimeMaxSec=" + str(math.ceil(duration + 400)),
                "--property=TimeoutStopSec=120",
                "--property=KillMode=mixed",
                *env,
                get("vm", "python"),
                str(root / "live-transcript.pyz"),
                "lab-guest",
                "run",
                str(root),
            ]
        )
        submitted = True
        deadline = time.monotonic() + duration + 320
        while time.monotonic() < deadline:
            probe = (
                remote(
                    [
                        get("vm", "python"),
                        "-c",
                        "import pathlib,sys; print((pathlib.Path(sys.argv[1])/'result.json').exists())",
                        str(root),
                    ]
                )
                .decode()
                .strip()
            )
            if probe == "True":
                break
            active = subprocess.run(
                [*ssh(), shlex.join(["systemctl", "--user", "is-active", receipt["unit"]])],
                capture_output=True,
                timeout=10,
            )
            if active.returncode:
                # The result can be published between our first stat and the
                # unit-state query. Recheck before classifying a stopped unit.
                final_probe = (
                    remote(
                        [
                            get("vm", "python"),
                            "-c",
                            "import pathlib,sys; print((pathlib.Path(sys.argv[1])/'result.json').exists())",
                            str(root),
                        ]
                    )
                    .decode()
                    .strip()
                )
                if final_probe == "True":
                    break
                raise RuntimeError("Guest lab stopped before publishing a result")
            time.sleep(1)
        else:
            raise TimeoutError("Guest lab exceeded its bounded runtime")
    finally:
        if submitted:
            try:
                remote(
                    [
                        "env",
                        "LIVE_TRANSCRIPT_CONFIG=" + str(root / "config.toml"),
                        get("vm", "python"),
                        str(root / "live-transcript.pyz"),
                        "lab-guest",
                        "cleanup",
                        str(root),
                    ],
                    timeout=215,
                )
            except subprocess.SubprocessError as exc:
                cleanup_error = type(exc).__name__
        if caption and caption.poll() is None:
            drain_caption(host, port, session, case / "drain.json")
        if observer:
            observer.close()
        if tunnel:
            stop_process(tunnel)
        if caption:
            stop_process(caption)
        if engine:
            stop_process(engine)
        atomic_json(case / "cleanup.json", dict(ok=cleanup_error is None, error=cleanup_error))
        try:
            journal = remote(
                [
                    "journalctl",
                    "--user",
                    "--unit",
                    receipt["unit"],
                    "-n",
                    "120",
                    "--no-pager",
                    "--output=short-iso",
                ]
            )
            (case / "guest-runner.log").write_bytes(journal[: 1024 * 1024])
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            collect(case, receipt)
        except Exception as exc:
            atomic_json(
                case / "retrieval-error.json", dict(error=type(exc).__name__, detail=str(exc)[:500])
            )
    if cleanup_error:
        raise RuntimeError(
            "Lab cleanup could not be confirmed; retain the receipt and inspect its exact unit"
        )
    # The journal is closed after capture/tail drain; read only this lab's state.
    value = assess(case)
    if not value.get("capture_ok"):
        return value
    if final:
        wav = case / value["recording"]
        environment = profile_environment(profile)
        run_owned(
            command(
                "lab-final",
                str(wav),
                str(case / "final-transcript.json"),
                "--speakers",
                str(case / "guest/rec" / (session + ".archive") / "speakers.jsonl"),
            ),
            environment,
            case / "final.log",
            get("postprocess", "timeout_seconds") * 8,
        )
        final_result = json.loads((case / "final-transcript.json").read_text())
        value["final"] = evaluation.score(
            json.loads((case / "reference.json").read_text()), final_result["lines"]
        )
        value["final_recognition_seconds"] = final_result["recognition_seconds"]
    return value


def run(args):
    if args.lab_action != "jitsi":
        return _run(args)
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(*_):
        raise InterruptedError("Lab termination requested")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        return _run(args)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run(args):
    if args.lab_action == "collect":
        case = Path(args.directory).expanduser().resolve()
        receipt = json.loads((case / "receipt.json").read_text())
        collect(case, receipt)
        value = assess(case)
        atomic_json(case / "score.json", value)
        return value
    if args.lab_action == "report":
        output = Path(args.output).expanduser().resolve()
        if output.exists() or output in {
            Path(args.reference).resolve(),
            Path(args.transcript).resolve(),
        }:
            raise ValueError("Offline score output must be a new file")
        reference = json.loads(Path(args.reference).read_text())
        transcript = json.loads(Path(args.transcript).read_text())
        if isinstance(transcript, dict) and isinstance(transcript.get("lines"), list):
            transcript = transcript["lines"]
        value = evaluation.score(reference, transcript)
        if args.baseline:
            value["comparison"] = evaluation.compare(
                value, json.loads(Path(args.baseline).read_text())
            )
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(output, value)
        return value
    server = server_url(args.server)
    if args.silence_finalize is not None and (
        not math.isfinite(args.silence_finalize) or not 0.5 <= args.silence_finalize <= 10
    ):
        raise ValueError("Lab silence boundary must be 0.5–10 seconds")
    root = Path(args.output).expanduser().absolute()
    if root.exists():
        raise ValueError(
            "Lab output must be a new directory; existing evidence is never overwritten"
        )
    root.mkdir(parents=True, mode=0o700)
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    report = dict(
        schema=1,
        ok=False,
        scenarios={},
        started=time.time(),
        notes="Completion means captured and scored, not certified accurate. Original production services and audio topology are unchanged.",
    )
    if args.endpoint and args.asr_mode != "existing":
        raise ValueError("Select an existing endpoint or an owned inference server, not both")
    readiness = (
        preflight_asr(args.endpoint)
        if args.asr_mode == "existing"
        else dict(
            ok=all(Path(get("paths", key)).is_file() for key in ("whisper_server", "model", "vad")),
            mode=args.asr_mode,
        )
    )
    atomic_json(root / "asr-readiness.json", readiness)
    if not readiness["ok"]:
        report.update(error="No configured speech endpoint is ready", stage="preflight")
        atomic_json(root / "report.json", report)
        return report
    from live_transcript.runtime.state import current

    if current().get("status") in ("active", "control_lost"):
        raise RuntimeError("A meeting may still be active; inspect it before testing")
    with ExitStack() as leases:
        lock_path = Path(get("paths", "logs")) / "run.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = leases.enter_context(lock_path.open("a"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        phrases = synthesize(root / "source-audio", args.plan)
        for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
            print("LAB", scenario, "starting", flush=True, file=sys.stderr)
            try:
                value = run_case(
                    root,
                    server,
                    scenario,
                    phrases,
                    root / "source-audio",
                    endpoint=args.endpoint,
                    final=not args.no_final,
                    asr_mode=args.asr_mode,
                    silence_finalize=args.silence_finalize,
                )
                if baseline:
                    previous = baseline.get("scenarios", {}).get(scenario, {})
                    value["comparison"] = {}
                    for mode in ("live", "final"):
                        if mode in value:
                            try:
                                value["comparison"][mode] = evaluation.compare(
                                    value[mode], previous.get(mode, {})
                                )
                            except ValueError as exc:
                                value["comparison"][mode] = dict(ok=False, error=str(exc))
                    value["ok"] = value["ok"] and all(v["ok"] for v in value["comparison"].values())
                report["scenarios"][scenario] = value
                atomic_json(root / scenario / "score.json", value)
            except Exception as exc:
                report["scenarios"][scenario] = dict(
                    ok=False, error=type(exc).__name__, detail=str(exc)[:500]
                )
            atomic_json(root / "report.json", report)
            atomic_text(root / "report.md", evaluation.markdown(report))
            if not report["scenarios"][scenario].get("capture_ok"):
                break
    report.update(
        ok=bool(report["scenarios"]) and all(v["ok"] for v in report["scenarios"].values()),
        finished=time.time(),
    )
    atomic_json(root / "report.json", report)
    return report


if __name__ == "__main__":
    guarded_child()
