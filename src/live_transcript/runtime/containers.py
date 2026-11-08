"""Optional Compose supervision for CPU speech services; browsers stay in VMs."""

from importlib.resources import files
import hashlib
import json
import ipaddress
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit

from live_transcript.core.files import atomic_json, atomic_text


def export_options(parser):
    parser.add_argument("directory", help="new, empty directory for the portable backend bundle")
    parser.add_argument(
        "--models", required=True, help="directory containing verified whisper weights"
    )
    parser.add_argument(
        "--state",
        required=True,
        help="local durable journal directory, shared with the host profile",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="host IP for caption TCP; use the VM bridge IP for guest access",
    )
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--threads",
        type=int,
        default=max(1, min(8, os.cpu_count() or 2)),
        help="CPU threads/quota per speech service",
    )
    parser.add_argument(
        "--download-models", action="store_true", help="download/check pinned public model weights"
    )
    parser.add_argument(
        "--model-url", default="", help="optional notes endpoint reachable from containers"
    )
    parser.add_argument("--model-name", default="local-model")


def backend_configuration(model_dir, state_dir, bind, port, ident, uid, gid, threads=8):
    address = ipaddress.ip_address(bind)
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError("Use a specific loopback or trusted VM bridge IPv4 address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid caption port")
    if type(threads) is not int or not 1 <= threads <= 256:
        raise ValueError("Speech threads must be between 1 and 256")
    result = configuration(model_dir, [19091, 19092, 19093], ident)
    for service in result["services"].values():
        service.pop("ports")  # speech endpoints exist only on the private Compose network
        service["command"][-1] = str(threads)
        service["cpus"] = threads
    result["name"] = "live-transcript-" + ident
    result["services"]["captions"] = dict(
        image="live-transcript-backend:" + ident,
        pull_policy="never",
        build=dict(context=".", dockerfile="backend.Dockerfile"),
        user=f"{uid}:{gid}",
        ports=[f"{bind}:{port}:8099"],
        volumes=[
            dict(
                type="bind",
                source=str(Path(state_dir).absolute()).replace("$", "$$"),
                target="/state",
            ),
            dict(
                type="bind", source="./backend.toml", target="/config/backend.toml", read_only=True
            ),
        ],
        read_only=True,
        tmpfs=["/tmp:size=256m,mode=1777"],
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        init=True,
        restart="unless-stopped",
        stop_grace_period="30s",
        pids_limit=256,
        mem_limit="2g",
        # Caption ingest must start even while speech is unavailable. Its journal
        # acknowledges durable packets; a model outage must not block audio ingest.
        healthcheck=dict(
            test=[
                "CMD",
                "python3",
                "-c",
                "import socket,json; s=socket.create_connection(('127.0.0.1',8099),2); "
                "s.settimeout(2); s.sendall(b'GETGPU\\n'); "
                "assert isinstance(json.loads(s.makefile('rb').readline(65536)),dict)",
            ],
            interval="10s",
            timeout="3s",
            retries=3,
            start_period="15s",
        ),
        labels={"io.live-transcript.installation": ident},
    )
    return result


def export_backend(args):
    """Export an independent CPU backend; never edit the selected host profile."""
    from live_transcript.runtime.deployment import build_bundle
    from live_transcript.runtime.installation import MODELS
    from live_transcript.runtime.provision import download, sha256

    directory = Path(args.directory).expanduser().absolute()
    models, state = (
        Path(args.models).expanduser().absolute(),
        Path(args.state).expanduser().absolute(),
    )
    ident = hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    uid, gid = getattr(os, "getuid", lambda: 10001)(), getattr(os, "getgid", lambda: 10001)()
    if uid == 0:
        raise ValueError("Export as the unprivileged user who owns the journal directory")
    content = backend_configuration(
        models, state, args.bind, args.port, ident, uid, gid, args.threads
    )
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(
            "Export requires an empty directory; existing deployments are never overwritten"
        )
    if args.model_url:
        parsed = urlsplit(args.model_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
        ):
            raise ValueError("Use an HTTP(S) model endpoint without credentials, query or fragment")
    # Verify before creating deployment files. Weights are immutable public data;
    # only they are made readable to the non-root speech uid.
    if args.download_models:
        models.mkdir(parents=True, exist_ok=True)
    for repo, revision, filename, checksum in MODELS.values():
        path = models / filename
        if path.is_symlink():
            raise ValueError("Model weights must be regular files, not symlinks")
        if args.download_models:
            download(f"https://huggingface.co/{repo}/resolve/{revision}/{filename}", path, checksum)
        elif not path.is_file() or sha256(path) != checksum:
            raise ValueError("Missing or unverified model: " + filename + "; use --download-models")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_uid != uid or not os.access(state, os.W_OK | os.X_OK):
        raise ValueError("The current user must own the writable journal directory")
    models.chmod(models.stat().st_mode | 0o055)
    for _, _, filename, _ in MODELS.values():
        path = models / filename
        path.chmod(path.stat().st_mode | 0o044)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    build_bundle(directory / "backend.pyz")
    resources = files("live_transcript.runtime")
    for name in ("backend.Dockerfile", "whisper.Dockerfile"):
        atomic_text(directory / name, resources.joinpath(name).read_text())
    atomic_json(directory / "compose.json", content)
    atomic_text(
        directory / ".dockerignore", "*\n!backend.pyz\n!backend.Dockerfile\n!whisper.Dockerfile\n"
    )
    settings = {
        "paths": dict(root="/state", state="/state"),
        "caption": dict(bind="0.0.0.0", port=8099, stt_url="http://primary:8080/inference"),
        "asr": dict(
            primary_device="cpu",
            cpu_url="http://primary:8080/inference",
            small_cpu_url="http://cpu_small:8080/inference",
        ),
        "model": dict(
            enabled=bool(args.model_url),
            url=args.model_url or "http://127.0.0.1:9",
            name=args.model_name,
        ),
        "services": dict(captions="", primary="", cpu_large="", cpu_small=""),
    }
    atomic_text(
        directory / "backend.toml",
        "\n\n".join(
            "["
            + section
            + "]\n"
            + "\n".join(key + " = " + json.dumps(value) for key, value in values.items())
            for section, values in settings.items()
        )
        + "\n",
    )
    return dict(
        directory=str(directory),
        compose=["docker", "compose", "-f", str(directory / "compose.json")],
        caption_host=args.bind,
        caption_port=args.port,
        state=str(state),
        started=False,
        notes_enabled=bool(args.model_url),
    )


def configuration(model_dir, ports, ident):
    if (
        len(ports) != 3
        or len(set(ports)) != 3
        or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
    ):
        raise ValueError("Compose requires three distinct valid speech ports")
    model_dir = Path(model_dir).absolute()
    services = {}
    for key, port in zip(("primary", "cpu_large", "cpu_small"), ports):
        if key == "cpu_large":
            continue  # reuse the full CPU primary instead of loading it twice
        model = "ggml-small.bin" if key == "cpu_small" else "ggml-large-v3.bin"
        services[key] = dict(
            image="live-transcript-speech:" + ident,
            pull_policy="never",
            command=[
                "-m",
                "/models/" + model,
                "--vad",
                "-vm",
                "/models/ggml-silero-v6.2.0.bin",
                "--no-gpu",
                "-l",
                "auto",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
                "-t",
                "8",
            ],
            ports=[f"127.0.0.1:{port}:8080"],
            volumes=[
                dict(
                    type="bind",
                    source=str(model_dir).replace("$", "$$"),
                    target="/models",
                    read_only=True,
                )
            ],
            user="10001:10001",
            read_only=True,
            tmpfs=["/tmp:size=256m,mode=1777"],
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            init=True,
            restart="unless-stopped",
            stop_grace_period="30s",
            pids_limit=256,
            mem_limit="8g",
            healthcheck=dict(
                test=[
                    "CMD",
                    "python3",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2)",
                ],
                interval="10s",
                timeout="3s",
                retries=6,
                start_period="180s",
            ),
            labels={"io.live-transcript.installation": ident},
        )
        if key == "primary":
            services[key]["build"] = dict(context=".", dockerfile="whisper.Dockerfile")
    return dict(services=services)


def install(runner, directory, model_dir, ports, ident, start=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Docker reads models as a different uid. Only immutable public weights are
    # shared; state, credentials and recordings never enter a build or container.
    Path(model_dir).chmod(0o755)
    for path in Path(model_dir).glob("*.bin"):
        path.chmod(0o644)
    content = configuration(model_dir, ports, ident)
    path = directory / "compose.json"
    receipt = directory / "compose.sha256"
    if path.exists() and json.loads(path.read_text()) != content:
        if (
            not receipt.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.read_text().strip()
        ):
            raise ValueError("Generated Compose configuration was edited; refusing to overwrite it")
    atomic_json(path, content)
    atomic_text(receipt, hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
    atomic_text(
        directory / "whisper.Dockerfile",
        files("live_transcript.runtime").joinpath("whisper.Dockerfile").read_text(),
    )
    atomic_text(directory / ".dockerignore", "*\n!whisper.Dockerfile\n")
    prefix = ["docker"]
    if runner.run([*prefix, "info"], check=False, capture=True, timeout=20).returncode:
        prefix = ["sudo", "-n", "docker"]
        runner.run([*prefix, "info"], capture=True, timeout=20)
    command = [*prefix, "compose", "--project-name", "live-transcript-" + ident, "-f", str(path)]
    runner.run([*command, "config", "--quiet"])
    # One build owns the shared image tag; parallel builds can race in BuildKit.
    runner.run([*command, "build", "primary"], timeout=1800)
    if start:
        runner.run([*command, "up", "-d", "--wait", "--wait-timeout", "300"], timeout=360)
        # Host reachability is separate from Compose's in-container health.
        deadline = time.monotonic() + 30
        for port in (ports[0], ports[2]):
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Compose speech endpoint is not reachable from the host")
                    time.sleep(1)
    return command
