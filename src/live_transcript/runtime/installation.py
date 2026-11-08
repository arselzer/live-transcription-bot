"""Repeatable standalone installation, with an isolated managed browser VM.

State, keys, models and logs stay outside the source checkout. Installation owns
only its recorded VM, profile and named service units; reruns never adopt or
restart another deployment. Optional inference services may be managed externally.
"""

import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import threading
import urllib.request
import uuid

from live_transcript.core.files import atomic_json, atomic_text
from live_transcript.runtime.provision import BrowserVM, Runner, download, sha256

WHISPER_REVISION = "d1be6fde11ac6e0407606b4e42fe72d34add8037"  # upstream v1.9.5
WEIGHTS_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
VAD_REVISION = "9ffd54a1e1ee413ddf265af9913beaf518d1639b"
MODELS = {
    "model": (
        "ggerganov/whisper.cpp",
        WEIGHTS_REVISION,
        "ggml-large-v3.bin",
        "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
    ),
    "small_model": (
        "ggerganov/whisper.cpp",
        WEIGHTS_REVISION,
        "ggml-small.bin",
        "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    ),
    "vad": (
        "ggml-org/whisper-vad",
        VAD_REVISION,
        "ggml-silero-v6.2.0.bin",
        "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
    ),
}
PACKAGES = {
    "virsh": "libvirt-clients",
    "qemu-system-x86_64": "qemu-system-x86",
    "qemu-img": "qemu-utils",
    "cloud-localds": "cloud-image-utils",
    "ffmpeg": "ffmpeg",
    "ssh": "openssh-client",
    "ssh-keygen": "openssh-client",
    "rsync": "rsync",
    "nc": "netcat-openbsd",
    "cmake": "cmake",
    "c++": "build-essential",
    "git": "git",
    "flock": "util-linux",
}


@contextmanager
def sudo_session(runner):
    """Refresh the operator's existing sudo timestamp during long downloads/builds."""
    runner.run(["sudo", "-n", "-v"])
    stopped = threading.Event()

    def refresh():
        while not stopped.wait(60):
            try:
                result = subprocess.run(
                    ["sudo", "-n", "-v"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                if result.returncode:
                    return  # the next privileged command reports the expired authorization
            except (OSError, subprocess.SubprocessError):
                return

    worker = threading.Thread(target=refresh, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=11)


def default_profile():
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "live-transcript/config.toml"
    )


def setup_options(parser):
    parser.add_argument(
        "--plan", action="store_true", help="show the installation plan without changes"
    )
    parser.add_argument("--workspace", type=Path, help="private local data/model/setup directory")
    parser.add_argument("--vm-name", help="new owned libvirt domain name")
    parser.add_argument("--network", default="default", help="existing libvirt NAT/DHCP network")
    parser.add_argument("--memory-mib", type=int, default=4096)
    parser.add_argument("--vcpus", type=int, default=2)
    parser.add_argument("--disk-gib", type=int, default=64)
    parser.add_argument("--backend", choices=("cpu", "cuda", "hip"), default="cpu")
    parser.add_argument(
        "--speech-supervisor",
        choices=("systemd", "compose"),
        default="systemd",
        help="host speech supervision; the browser always stays in its VM",
    )
    parser.add_argument(
        "--vm-storage",
        type=Path,
        default=Path("/var/lib/libvirt/images"),
        help="local libvirt VM disk directory",
    )
    parser.add_argument("--build-jobs", type=int, default=2)
    parser.add_argument(
        "--recording-only",
        action="store_true",
        help="provision recording without speech models/services",
    )
    parser.add_argument(
        "--no-start-services",
        action="store_true",
        help="install service units without starting them",
    )


def specification(args, profile):
    profile = Path(profile or default_profile()).expanduser().absolute()
    ident = hashlib.sha256(str(profile).encode()).hexdigest()[:10]
    workspace = args.workspace or Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    ) / ("live-transcript-" + ident)
    workspace = workspace.expanduser().absolute()
    if (
        not 2048 <= args.memory_mib <= 65536
        or not 1 <= args.vcpus <= 32
        or not 12 <= args.disk_gib <= 2048
    ):
        raise ValueError("VM limits: 2048–65536 MiB, 1–32 vCPUs, 12–2048 GiB disk")
    if not 1 <= args.build_jobs <= 16:
        raise ValueError("--build-jobs must be between 1 and 16")
    options = dict(
        vm_name=args.vm_name or "live-transcript-" + ident,
        network=args.network,
        memory_mib=args.memory_mib,
        vcpus=args.vcpus,
        disk_gib=args.disk_gib,
        backend=args.backend,
        build_jobs=args.build_jobs,
        recording_only=args.recording_only,
        speech_supervisor=args.speech_supervisor,
        vm_storage=str(args.vm_storage.expanduser().absolute()),
    )
    if options["speech_supervisor"] == "compose" and options["backend"] != "cpu":
        raise ValueError(
            "The bundled Compose speech image currently supports CPU; use systemd for CUDA/HIP"
        )
    for name in (options["vm_name"], options["network"]):
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,55}", name):
            raise ValueError(
                "VM and network names must use 1–56 simple letters, digits, dash or underscore"
            )
    if any(ord(c) < 32 for c in str(workspace) + str(profile)):
        raise ValueError("Control characters are not allowed in installation paths")
    return profile, workspace, ident, options


def toml(sections):
    return (
        "\n".join(
            "["
            + section
            + "]\n"
            + "\n".join(key + " = " + json.dumps(value) for key, value in entries.items())
            for section, entries in sections.items()
        )
        + "\n"
    )


def systemd_quote(value):
    # Systemd command syntax differs from shell syntax; %% escapes specifiers.
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError("Invalid systemd command value")
    return json.dumps(value.replace("%", "%%"))


def service_unit(profile, ident, args):
    return (
        "# Managed by live-transcript setup: " + ident + "\n"
        "[Unit]\nDescription=Live Transcript " + ident + "\nAfter=network.target\n"
        "StartLimitIntervalSec=0\n[Service]\n"
        "ExecStart="
        + " ".join(
            map(
                systemd_quote,
                [sys.executable, "-m", "live_transcript", "--config", str(profile), "serve", *args],
            )
        )
        + "\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nUMask=0077\n"
        "[Install]\nWantedBy=default.target\n"
    )


def unused_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class Installation:
    def __init__(self, profile, root, ident, options, no_start=False):
        self.profile, self.root, self.ident, self.options = profile, root, ident, options
        self.no_start = no_start
        self.state_path = root / "installation.json"
        self.runner = Runner(root / "installation.log")
        self.state = {}

    def save(self, phase, **fields):
        self.state.update(phase=phase, utc=time.time(), **fields)
        atomic_json(self.state_path, self.state)
        print("SETUP", phase, flush=True)

    def claim(self):
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if (
                not isinstance(self.state, dict)
                or self.state.get("version") != 1
                or not isinstance(self.state.get("options"), dict)
                or not re.fullmatch(r"[a-f0-9]{32}", str(self.state.get("id", "")))
            ):
                raise ValueError(
                    "Invalid or unsupported installation state; original resources were not changed"
                )
            # Early managed installations used these same implicit defaults.
            self.state.get("options", {}).setdefault("speech_supervisor", "systemd")
            self.state.get("options", {}).setdefault("vm_storage", "/var/lib/libvirt/images")
            if (
                self.state.get("profile") != str(self.profile)
                or self.state.get("options") != self.options
            ):
                raise ValueError(
                    "Workspace belongs to a different setup plan; reuse its options or choose a new workspace"
                )
            expected = self.state.get("profile_sha256")
            if self.profile.exists() and (not expected or sha256(self.profile) != expected):
                raise ValueError(
                    "Managed profile was changed; preserve it and choose a new setup profile"
                )
        elif self.profile.exists():
            raise ValueError(
                "Setup refuses to replace an existing profile; choose --config with a new filename"
            )
        else:
            if {path.name for path in self.root.iterdir()} - {"setup.lock", "downloads"}:
                raise ValueError("Setup requires an empty workspace or its own installation state")
            token = uuid.uuid4().hex
            self.state = dict(
                version=1,
                id=token,
                profile=str(self.profile),
                options=self.options,
                created=time.time(),
                mac="52:54:00:" + ":".join(token[i : i + 2] for i in (0, 2, 4)),
                ports=unused_ports(4),
            )
            self.save("claimed")

    def prerequisites(self):
        if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
            raise ValueError("Managed provisioning currently supports Linux x86-64 with KVM")
        if os.geteuid() == 0:
            raise ValueError("Run setup as your normal user, with sudo available")
        if not Path("/dev/kvm").exists():
            raise ValueError(
                "KVM is unavailable; enable virtualization or use a prepared remote VM"
            )
        self.runner.run(["sudo", "-n", "true"])
        missing = sorted(
            {package for command, package in PACKAGES.items() if not shutil.which(command)}
        )
        if self.options["speech_supervisor"] == "compose" and not shutil.which("docker"):
            missing.extend(["docker.io", "docker-compose-v2"])
        if missing or not Path("/etc/libvirt").is_dir():
            if not shutil.which("apt-get"):
                raise ValueError(
                    "Automatic host dependency installation currently supports apt-based systems"
                )
            self.runner.run(["sudo", "-n", "apt-get", "update"])
            self.runner.run(
                [
                    "sudo",
                    "-n",
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "apt-get",
                    "install",
                    "-y",
                    *sorted(
                        set(missing)
                        | {"libvirt-daemon-system", "libvirt-daemon-config-network", "libssl-dev"}
                    ),
                ],
                timeout=1200,
            )
        self.runner.run(["systemctl", "--user", "show-environment"], capture=True)
        if self.runner.virsh("list", "--all", check=False).returncode:
            units = self.runner.run(
                [
                    "systemctl",
                    "list-unit-files",
                    "--no-legend",
                    "libvirtd.socket",
                    "virtqemud.socket",
                ],
                capture=True,
            ).stdout.decode()
            unit = "virtqemud.socket" if "virtqemud.socket" in units else "libvirtd.socket"
            self.runner.run(["sudo", "-n", "systemctl", "start", unit])
            self.runner.virsh("list", "--all")
        if shutil.disk_usage(self.root).free < 12 * 1024**3:
            raise ValueError(
                "At least 12 GiB free local space is required for setup; allow more for meetings"
            )
        self.save("host_ready")

    def speech(self):
        if self.options["recording_only"]:
            return
        source = self.root / "whisper.cpp"
        if not source.exists():
            self.runner.run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    "https://github.com/ggml-org/whisper.cpp.git",
                    source,
                ]
            )
            self.runner.run(["git", "-C", source, "checkout", "--detach", WHISPER_REVISION])
        current = self.runner.run(
            ["git", "-C", source, "status", "--porcelain"], capture=True
        ).stdout
        if current.strip():
            raise ValueError(
                "Managed whisper.cpp source has local changes; refusing to discard them"
            )
        self.runner.run(["git", "-C", source, "checkout", "--detach", WHISPER_REVISION])
        flags = [
            "-DGGML_CUDA=" + ("ON" if self.options["backend"] == "cuda" else "OFF"),
            "-DGGML_HIP=" + ("ON" if self.options["backend"] == "hip" else "OFF"),
        ]
        self.runner.run(
            ["cmake", "-S", source, "-B", source / "build", "-DCMAKE_BUILD_TYPE=Release", *flags]
        )
        self.runner.run(
            [
                "cmake",
                "--build",
                source / "build",
                "--target",
                "whisper-cli",
                "whisper-server",
                "--parallel",
                self.options["build_jobs"],
            ],
            timeout=1800,
        )
        for repo, revision, filename, checksum in MODELS.values():
            print("SETUP verifying model", filename, flush=True)
            download(
                f"https://huggingface.co/{repo}/resolve/{revision}/{filename}",
                self.root / "models" / filename,
                checksum,
            )
        self.save("speech_ready")

    def profile_config(self, ip, gateway):
        caption, primary, cpu_large, cpu_small = self.state["ports"]
        services = {
            key: "live-transcript-" + self.ident + "-" + key + ".service"
            for key in ("captions", "primary", "cpu_large", "cpu_small")
        }
        if self.options["speech_supervisor"] == "compose":
            services.update(primary="", cpu_large="", cpu_small="")
        elif self.options["backend"] == "cpu":
            services["cpu_large"] = ""  # the full primary is already a CPU service
        paths = dict(
            root=str(self.root / "data"),
            state=str(self.root / "data/state"),
            whisper_cli=str(self.root / "whisper.cpp/build/bin/whisper-cli"),
            whisper_server=str(self.root / "whisper.cpp/build/bin/whisper-server"),
            model=str(self.root / "models/ggml-large-v3.bin"),
            small_model=str(self.root / "models/ggml-small.bin"),
            vad=str(self.root / "models/ggml-silero-v6.2.0.bin"),
            postprocess_python=sys.executable,
        )
        sections = dict(
            paths=paths,
            vm=dict(
                name=self.options["vm_name"],
                ssh="transcript@" + ip,
                ip=ip,
                home="/home/transcript",
                python="/opt/live-transcript/venv/bin/python",
                manage_libvirt=True,
                identity_file=str(self.root / "ssh-key"),
                known_hosts=str(self.root / "known_hosts"),
            ),
            services=services,
            caption=dict(
                host=gateway,
                bind=gateway,
                port=caption,
                stt_url=f"http://127.0.0.1:{primary}/inference",
            ),
            asr=dict(
                primary_device="cpu" if self.options["backend"] == "cpu" else "gpu",
                cpu_url=f"http://127.0.0.1:{primary if self.options['backend'] == 'cpu' else cpu_large}/inference",
                small_cpu_url=f"http://127.0.0.1:{cpu_small}/inference",
                cpu_threads=max(1, min(8, (os.cpu_count() or 2) // 2)),
            ),
        )
        if self.options["recording_only"]:
            sections["asr"].update(cpu_fallback=False, adaptive_model=False)
            sections["services"] = {key: "" for key in services}
        self.profile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = toml(sections)
        if not self.profile.exists():
            self.save(
                "writing_profile", profile_sha256=hashlib.sha256(content.encode()).hexdigest()
            )
            atomic_text(self.profile, content)
        self.save("profile_ready", profile_sha256=sha256(self.profile), ip=ip, gateway=gateway)
        return sections

    def privileges(self):
        name = pwd.getpwuid(os.getuid()).pw_name
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", name):
            raise ValueError("Unsupported account name for managed sudo policy")
        vm = self.options["vm_name"]
        content = f"{name} ALL=(root) NOPASSWD: /usr/bin/virsh start {vm}, /usr/bin/virsh domifaddr {vm}\n"
        path = self.root / "sudo-policy"
        atomic_text(path, content)
        self.runner.run(["sudo", "-n", "visudo", "-cf", path])
        self.runner.run(
            [
                "sudo",
                "-n",
                "install",
                "-m",
                "0440",
                path,
                "/etc/sudoers.d/live-transcript-" + self.ident,
            ]
        )
        self.runner.run(["sudo", "-n", "loginctl", "enable-linger", name])

    def units(self, config):
        if self.options["recording_only"]:
            return
        directory = (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd/user"
        )
        directory.mkdir(parents=True, exist_ok=True)
        commands = dict(
            captions=["captions"],
            primary=["asr", "--device", "gpu"],
            cpu_large=["asr", "--device", "cpu-large"],
            cpu_small=["asr", "--device", "cpu-small"],
        )
        # Editable installs need their source root; installed packages use their interpreter.
        package = Path(__file__).resolve().parents[2]
        source = package if package.name == "src" else None
        for key, name in config["services"].items():
            if not name:
                continue
            path = directory / name
            expected = "# Managed by live-transcript setup: " + self.state["id"] + "\n"
            if path.exists() and not path.read_text().startswith(expected):
                raise ValueError("Service unit already exists outside this installation: " + name)
            content = service_unit(self.profile, self.state["id"], commands[key])
            if source:
                content = content.replace(
                    "[Service]\n",
                    "[Service]\nEnvironment=" + systemd_quote("PYTHONPATH=" + str(source)) + "\n",
                )
            digest = hashlib.sha256(content.encode()).hexdigest()
            hashes = self.state.setdefault("unit_hashes", {})
            pending = self.state.setdefault("unit_pending_hashes", {})
            if path.exists() and sha256(path) not in {hashes.get(name), pending.get(name), digest}:
                raise ValueError("Managed service unit was edited; refusing to replace " + name)
            pending[name] = digest
            self.save("writing_units")
            atomic_text(path, content)
            hashes[name] = digest
            pending.pop(name, None)
            self.save("writing_units")
        self.runner.run(["systemctl", "--user", "daemon-reload"])
        names = [name for name in config["services"].values() if name]
        if not self.no_start:
            self.runner.run(["systemctl", "--user", "enable", *names])
            # start is idempotent; rerunning setup must never interrupt a model or call.
            self.runner.run(["systemctl", "--user", "start", *names], timeout=300)
        self.save("services_installed", services_started=not self.no_start)

    def media(self, vm, ip):
        if self.state.get("media_verified"):
            return
        from live_transcript.runtime.deployment import build_bundle

        bundle = self.root / "live-transcript.pyz"
        build_bundle(bundle)
        # stdin and stdout carry bytes only over the already verified SSH channel.
        vm.ssh(ip, "cat > /home/transcript/live-transcript.pyz", input=bundle.read_bytes())
        env = dict(os.environ, LIVE_TRANSCRIPT_CONFIG=str(self.profile))
        config = self.root / "guest.toml"
        self.runner.run(
            [sys.executable, "-m", "live_transcript.worker", "config", "--vm-config", config],
            env=env,
        )
        vm.ssh(
            ip,
            "mkdir -p /home/transcript/.config/live-transcript && cat > /home/transcript/.config/live-transcript/config.toml",
            input=config.read_bytes(),
        )
        vm.ssh(
            ip,
            "export LIVE_TRANSCRIPT_CONFIG=/home/transcript/.config/live-transcript/config.toml; "
            "exec xvfb-run -a -s '-screen 0 1280x960x24' /opt/live-transcript/venv/bin/python -u /home/transcript/live-transcript.pyz check-media",
            timeout=120,
        )
        self.save("media_verified", media_verified=True)

    def health(self, vm, ip, config):
        if self.options["recording_only"] or self.no_start:
            return
        self.save("waiting_for_services")
        urls = {
            config["caption"]["stt_url"],
            config["asr"]["cpu_url"],
            config["asr"]["small_cpu_url"],
        }
        deadline = time.monotonic() + 300
        for url in urls:
            while True:
                try:
                    with urllib.request.urlopen(
                        url.rsplit("/", 1)[0] + "/health", timeout=3
                    ) as response:
                        if json.loads(response.read(8192)).get("status") != "ok":
                            raise ValueError("Speech model is not ready")
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Speech services did not become ready; inspect owned service logs"
                        )
                    time.sleep(2)
        code = (
            "import socket,json; s=socket.create_connection("
            + repr((config["caption"]["host"], config["caption"]["port"]))
            + ",timeout=5); s.sendall(b'GETGPU\\n'); assert 'util' in json.loads(s.makefile('rb').readline(65536)); s.close()"
        )
        # This also detects host firewall and wrong bridge-bind configuration.
        vm.ssh(ip, "python3 -c " + shlex.quote(code))
        self.save("services_verified", services_verified=True)

    def apply(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("Installation workspace must be a real local directory")
        with (self.root / "setup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.claim()
            try:
                logs = self.root / "data/logs"
                logs.mkdir(parents=True, exist_ok=True, mode=0o700)
                with (logs / "run.lock").open("a") as meeting_lock, sudo_session(self.runner):
                    try:
                        fcntl.flock(meeting_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as exc:
                        raise RuntimeError(
                            "A meeting owns this installation; run setup after it ends"
                        ) from exc
                    self.prerequisites()
                    vm = BrowserVM(self.runner, self.state, self.root, self.options)
                    gateway = vm.network()
                    self.save("creating_vm")
                    vm.create()
                    self.save("waiting_for_vm")
                    ip = vm.ready()
                    vm.assert_idle(ip)
                    vm.reserve_address(ip)
                    self.save("vm_ready", ip=ip, gateway=gateway)
                    self.speech()
                    config = self.profile_config(ip, gateway)
                    self.media(vm, ip)
                    self.privileges()
                    self.units(config)
                    if (
                        not self.options["recording_only"]
                        and self.options["speech_supervisor"] == "compose"
                    ):
                        from live_transcript.runtime.containers import install as install_containers

                        command = install_containers(
                            self.runner,
                            self.root / "compose",
                            self.root / "models",
                            self.state["ports"][1:],
                            self.ident,
                            start=not self.no_start,
                        )
                        self.save("compose_installed", compose_command=command)
                    self.health(vm, ip, config)
                    self.save("complete")
                    return dict(
                        ok=True,
                        profile=str(self.profile),
                        workspace=str(self.root),
                        vm=self.options["vm_name"],
                        recording_only=self.options["recording_only"],
                        next_command=shlex.join(
                            ["live-transcript", "--config", str(self.profile), "check"]
                        ),
                    )
            except Exception as exc:
                self.save("failed", error=type(exc).__name__ + ": " + str(exc))
                raise


def run(args, profile=None):
    profile, root, ident, options = specification(args, profile)
    if args.plan:
        return dict(
            profile=str(profile),
            workspace=str(root),
            options=options,
            changes=[
                "install missing host dependencies",
                "create an owned Ubuntu browser VM",
                "install browser/audio/video dependencies inside the VM",
                "download verified speech weights and build whisper.cpp"
                if not options["recording_only"]
                else "recording-only: speech setup omitted",
                "write a new private profile and installation-scoped service units",
            ],
            privileges="sudo for packages/libvirt; narrow VM start rule for unattended operation",
            storage="local by default; no archive server required",
            plan_only=True,
        )
    return Installation(profile, root, ident, options, args.no_start_services).apply()
