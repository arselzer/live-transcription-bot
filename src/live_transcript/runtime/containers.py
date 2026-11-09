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
from types import SimpleNamespace

from live_transcript.core.files import atomic_json, atomic_text


def application_options(parser):
    parser.add_argument(
        "directory", help="new empty directory for the complete application deployment"
    )
    parser.add_argument("--models", required=True)
    parser.add_argument("--data", required=True, help="persistent local application data directory")
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="caption host IPv4; prepared VMs need a reachable bridge IP",
    )
    parser.add_argument("--port", type=int, default=8099, help="caption TCP port")
    parser.add_argument(
        "--speech-port", type=int, default=9091, help="loopback-only primary speech port"
    )
    parser.add_argument("--small-speech-port", type=int, default=9093)
    parser.add_argument("--threads", type=int, default=max(1, min(8, os.cpu_count() or 2)))
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument(
        "--model-url", default="", help="notes endpoint reachable from the containers"
    )
    parser.add_argument("--model-name", default="local-model")
    parser.add_argument("--workspace-port", type=int, default=6080)
    parser.add_argument("--workspace-listen", default="127.0.0.1")
    parser.add_argument("--workspace-host", default="")
    parser.add_argument("--workspace-url", default="")
    parser.add_argument(
        "--managed-vm", action="store_true", help="provision an isolated KVM guest with Compose"
    )
    parser.add_argument("--vm-data", default="", help="private VM storage outside application data")
    parser.add_argument("--vm-memory-mib", type=int, default=4096)
    parser.add_argument("--vm-vcpus", type=int, default=2)
    parser.add_argument("--vm-disk-gib", type=int, default=64)
    parser.add_argument("--vm-ssh-port", type=int, default=22222)
    parser.add_argument("--kvm-gid", type=int, help="numeric host group owning /dev/kvm")


def export_application(args):
    from live_transcript.core.config import CONFIG, load
    from live_transcript.runtime.deployment import build_application_wheel

    vm = dict(CONFIG["vm"])
    if not args.managed_vm and (not vm["ssh"] or not vm["identity_file"] or not vm["known_hosts"]):
        raise ValueError(
            "Select a prepared VM profile with pinned SSH identity_file and known_hosts"
        )
    # Config port checks happen before weight downloads or writing deployment data.
    ports = [args.port, args.speech_port, args.small_speech_port, args.workspace_port]
    if args.managed_vm:
        ports.append(args.vm_ssh_port)
    if len(set(ports)) != len(ports) or any(
        type(p) is not int or not 1 <= p <= 65535 for p in ports
    ):
        raise ValueError("Caption and speech ports must be valid and distinct")
    data = Path(args.data).expanduser().absolute()
    directory = Path(args.directory).expanduser().absolute()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Export requires an empty directory")
    from live_transcript.runtime.console import viewer_url

    workspace_address = ipaddress.ip_address(args.workspace_listen)
    if (
        workspace_address.version != 4
        or workspace_address.is_unspecified
        and not args.workspace_host
    ):
        raise ValueError("Set an advertised workspace host for an unrestricted listener")
    viewer_url(args.workspace_host or args.workspace_listen, args.workspace_port, "validation")
    if args.workspace_url:
        from live_transcript.runtime.workspace import public_origin

        public_origin(args.workspace_url)
    vm_spec = None
    vm_data = None
    if args.managed_vm:
        from live_transcript.runtime.qemu import prepare

        if args.bind != "127.0.0.1":
            raise ValueError("Managed VM caption listener must bind 127.0.0.1")
        if args.port == 2222:
            raise ValueError("Caption port conflicts with internal VM SSH port 2222")
        vm_data = (
            Path(args.vm_data).expanduser().absolute()
            if args.vm_data
            else data.with_name(data.name + "-vm")
        )
        # Neither app data nor the build context may contain the VM disk/admin key.
        paths = (vm_data.resolve(), data.resolve(), directory.resolve())
        if any(
            a == b or a in b.parents or b in a.parents
            for a, b in ((paths[0], paths[1]), (paths[0], paths[2]))
        ):
            raise ValueError(
                "VM storage must be separate from application data and deployment files"
            )
        if args.kvm_gid is None:
            args.kvm_gid = Path("/dev/kvm").stat().st_gid
        if type(args.kvm_gid) is not int or not 0 <= args.kvm_gid <= 2147483647:
            raise ValueError("Invalid KVM group id")
        vm_spec = prepare(
            vm_data,
            memory_mib=args.vm_memory_mib,
            vcpus=args.vm_vcpus,
            disk_gib=args.vm_disk_gib,
            ssh_port=args.vm_ssh_port,
            caption_port=args.port,
        )
        vm.update(
            ssh="transcript@127.0.0.1",
            port=args.vm_ssh_port,
            ip="127.0.0.1",
            name="live-transcript-" + vm_spec["id"],
            home="/home/transcript",
            python="/opt/live-transcript/venv/bin/python",
            identity_file=str(vm_data / "credentials/operator"),
            known_hosts=str(vm_data / "credentials/known_hosts"),
        )
    elif args.vm_data or args.kvm_gid is not None:
        raise ValueError("VM storage/group options require --managed-vm")
    # All credentials stay outside the Docker build context. Never modify keys.
    credentials = []
    for key in ("identity_file", "known_hosts"):
        path = Path(vm[key])
        if not path.is_file() or path.is_symlink() or path.stat().st_uid != os.getuid():
            raise ValueError("VM SSH credentials must be owned regular files")
        credentials.append(
            dict(
                type="bind",
                source=str(path).replace("$", "$$"),
                target="/credentials/" + key,
                read_only=True,
            )
        )
        vm[key] = "/credentials/" + key
    vm["manage_libvirt"] = False
    backend_args = SimpleNamespace(**vars(args), state=str(data / "state"))
    result = export_backend(backend_args)
    compose = json.loads((directory / "compose.json").read_text())
    ident = compose["name"].removeprefix("live-transcript-")
    for key, port in (("primary", args.speech_port), ("cpu_small", args.small_speech_port)):
        compose["services"][key]["ports"] = [f"127.0.0.1:{port}:8080"]
    models = Path(args.models).expanduser().absolute()
    # A single local state mount is shared with caption ingest, not a network FS.
    compose["services"]["app"] = dict(
        image="live-transcript-app:" + ident,
        pull_policy="never",
        build=dict(
            context=".",
            dockerfile="application.Dockerfile",
            target="wheel",
            args=dict(APP_UID=str(os.getuid()), APP_GID=str(os.getgid())),
        ),
        user=f"{os.getuid()}:{os.getgid()}",
        network_mode="host",
        volumes=[
            dict(type="bind", source=str(data).replace("$", "$$"), target="/data"),
            dict(
                type="bind", source=str(models).replace("$", "$$"), target="/models", read_only=True
            ),
            dict(
                type="bind",
                source="./application.toml",
                target="/config/config.toml",
                read_only=True,
            ),
            *credentials,
        ],
        read_only=True,
        tmpfs=["/tmp:size=512m,mode=1777"],
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        init=True,
        restart="unless-stopped",
        stop_grace_period="45s",
        pids_limit=512,
        mem_limit="12g",
        cpus=args.threads,
        healthcheck=dict(
            test=["CMD", "live-transcript", "serve", "app", "--health"],
            interval="10s",
            timeout="4s",
            retries=3,
            start_period="15s",
        ),
        labels={"io.live-transcript.installation": ident},
    )
    sections = {
        "paths": dict(
            root="/data",
            state="/data/state",
            model="/models/ggml-large-v3.bin",
            small_model="/models/ggml-small.bin",
            vad="/models/ggml-silero-v6.2.0.bin",
            whisper_cli="/usr/local/bin/whisper-cli",
            postprocess_python="/usr/local/bin/python3",
        ),
        "vm": vm,
        "runtime": dict(supervisor="app", watchdog_seconds=30, calendar_seconds=0, max_jobs=8),
        "services": dict(captions="", primary="", cpu_large="", cpu_small=""),
        "caption": dict(
            host=args.bind,
            guest_host="live-transcript-host" if vm_spec else "",
            port=args.port,
            stt_url=f"http://127.0.0.1:{args.speech_port}/inference",
        ),
        "asr": dict(
            primary_device="cpu",
            cpu_url=f"http://127.0.0.1:{args.speech_port}/inference",
            small_cpu_url=f"http://127.0.0.1:{args.small_speech_port}/inference",
            cpu_threads=args.threads,
        ),
        "model": dict(
            enabled=bool(args.model_url),
            url=args.model_url or "http://127.0.0.1:9",
            name=args.model_name,
        ),
        **{
            key: CONFIG[key]
            for key in (
                "audio",
                "browser",
                "google",
                "providers",
                "admission",
                "recovery",
                "identity",
                "capacity",
                "storage",
            )
        },
    }
    profile = directory / "application.toml"
    atomic_text(
        profile,
        "\n\n".join(
            "[" + key + "]\n" + "\n".join(k + " = " + json.dumps(v) for k, v in values.items())
            for key, values in sections.items()
        )
        + "\n",
    )
    load(profile)  # reject a bad generated profile before any container is started
    build_application_wheel(directory)
    write_application_dockerfile(directory)
    atomic_text(
        directory / ".dockerignore",
        "*\n!*.whl\n!backend.pyz\n!application.Dockerfile\n!backend.Dockerfile\n!whisper.Dockerfile\n!vm.Dockerfile\n",
    )
    if vm_spec:
        from live_transcript.core.config import write_vm_config

        write_vm_config(directory / "vm-guest.toml")
        add_managed_vm(compose, directory, vm_data, vm_spec, args.kvm_gid, data / "state/vm-status")
    from copy import deepcopy

    workspace = deepcopy(compose["services"]["app"])
    workspace.pop("build", None)
    workspace.update(
        command=[
            "workspace",
            "--listen",
            args.workspace_listen,
            "--port",
            str(args.workspace_port),
            "--advertise-host",
            args.workspace_host or args.workspace_listen,
        ],
        mem_limit="768m",
        cpus=1,
        pids_limit=128,
        healthcheck=dict(
            test=["CMD", "live-transcript", "workspace", "--health"],
            interval="10s",
            timeout="4s",
            retries=3,
            start_period="60s",
        ),
    )
    if args.workspace_url:
        workspace["command"] += ["--public-url", args.workspace_url]
    # Workspace/library stay available while the VM is booting or unavailable.
    workspace.pop("depends_on", None)
    compose["services"]["workspace"] = workspace
    atomic_json(directory / "compose.json", compose)
    from live_transcript.runtime.stack import register

    register(directory)
    result.update(application=True, profile=str(profile), vm_managed=bool(vm_spec))
    if vm_data:
        result["vm_data"] = str(vm_data)
    return result


def write_application_dockerfile(directory):
    source_dockerfile = (
        files("live_transcript.runtime").joinpath("application.Dockerfile").read_text()
    )
    runtime, marker, _ = source_dockerfile.partition(
        "# Package reviewed source. Exported wheel deployments replace this stage."
    )
    if not marker:
        raise ValueError("Application image runtime stage is unavailable")
    atomic_text(
        directory / "application.Dockerfile",
        runtime
        + """# Install the reviewed wheel from the bounded build context.
FROM runtime AS wheel
USER 0:0
COPY *.whl /tmp/application/
RUN python3 -m pip install --no-cache-dir Pillow==13.0.0 /tmp/application/*.whl \\
    && rm -rf /tmp/application
USER 10001:10001
""",
    )


def add_managed_vm(compose, directory, storage, spec, kvm_gid, status=None):
    """A private sidecar gets KVM and disk; the controller gets only operator SSH."""
    ident = compose["name"].removeprefix("live-transcript-")
    atomic_json(directory / "vm.json", spec)
    atomic_text(
        directory / "vm.Dockerfile",
        files("live_transcript.runtime").joinpath("vm.Dockerfile").read_text(),
    )
    compose["services"]["vm"] = dict(
        image="live-transcript-vm:" + ident,
        pull_policy="never",
        build=dict(
            context=".",
            dockerfile="vm.Dockerfile",
            args=dict(APP_UID=str(os.getuid()), APP_GID=str(os.getgid())),
        ),
        user=f"{os.getuid()}:{os.getgid()}",
        group_add=[str(kvm_gid)],
        devices=["/dev/kvm:/dev/kvm"],
        ports=[f"127.0.0.1:{spec['ssh_port']}:2222"],
        volumes=[
            dict(type="bind", source=str(storage).replace("$", "$$"), target="/vm"),
            dict(type="bind", source="./vm.json", target="/config/vm.json", read_only=True),
            dict(
                type="bind", source="./vm-guest.toml", target="/config/guest.toml", read_only=True
            ),
        ],
        read_only=True,
        tmpfs=["/tmp:size=256m,mode=1777"],
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        init=True,
        restart="unless-stopped",
        stop_grace_period="150s",
        pids_limit=256,
        mem_limit=str(spec["memory_mib"] + 1024) + "m",
        cpus=spec["vcpus"],
        healthcheck=dict(
            test=["CMD", "python3", "-m", "live_transcript.runtime.qemu", "--health"],
            interval="10s",
            timeout="4s",
            retries=6,
            start_period="1800s",
        ),
        labels={"io.live-transcript.installation": ident},
    )
    if status is not None:
        status.mkdir(parents=True, exist_ok=True, mode=0o700)
        compose["services"]["vm"]["volumes"].append(
            dict(type="bind", source=str(status).replace("$", "$$"), target="/status")
        )
        compose["services"]["vm"]["command"] = ["--status-directory", "/status"]
    # Startup ordering only: never propagate controller/ASR restarts to the VM.
    compose["services"]["app"]["depends_on"] = {"vm": {"condition": "service_healthy"}}


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
