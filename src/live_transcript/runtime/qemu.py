"""Owned QEMU sidecar: persistent guest, pinned SSH and graceful shutdown.

The controller never gets the VM disk, administrative key or QMP socket. A
controller/ASR outage does not stop this process or the independent guest recorder.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import time
import uuid

from live_transcript.core.files import atomic_json, atomic_text
from live_transcript.runtime.provision import IMAGE_SHA256, IMAGE_URL, cloud_config, download


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if path.resolve() != path:
        raise ValueError("VM storage must not contain symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("VM storage must be owned by the exporting user with mode 0700")
    return path


def regular_files(root):
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or (not stat.S_ISREG(mode) and not stat.S_ISDIR(mode)):
            # QMP is an owned Unix socket; the runner removes it after shutdown.
            if path.name != "qmp.sock" or not stat.S_ISSOCK(mode):
                raise ValueError("VM storage contains a link or special file: " + path.name)
        if path.lstat().st_uid != os.getuid():
            raise ValueError("VM storage contains files owned by another user")


def validate_spec(spec):
    expected = {
        "version",
        "id",
        "memory_mib",
        "vcpus",
        "disk_gib",
        "ssh_port",
        "caption_port",
        "image_url",
        "image_sha256",
    }
    if (
        not isinstance(spec, dict)
        or set(spec) != expected
        or type(spec["version"]) is not int
        or spec["version"] != 1
    ):
        raise ValueError("Unsupported VM specification")
    if str(uuid.UUID(spec["id"])) != spec["id"]:
        raise ValueError("Invalid VM owner identifier")
    for key, lower, upper in (
        ("memory_mib", 2048, 131072),
        ("vcpus", 1, 64),
        ("disk_gib", 24, 2048),
        ("ssh_port", 1024, 65535),
        ("caption_port", 1024, 65535),
    ):
        if type(spec[key]) is not int or not lower <= spec[key] <= upper:
            raise ValueError("Invalid VM " + key)
    if not spec["image_url"].startswith("https://cloud-images.ubuntu.com/") or (
        len(spec["image_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in spec["image_sha256"])
    ):
        raise ValueError("VM requires a pinned Ubuntu cloud image")
    return spec


def prepare(root, *, memory_mib, vcpus, disk_gib, ssh_port, caption_port):
    """Export credentials/spec without booting, adopting or replacing a disk."""
    root = Path(root).expanduser().absolute()
    values = dict(
        version=1,
        id=str(uuid.uuid4()),
        memory_mib=memory_mib,
        vcpus=vcpus,
        disk_gib=disk_gib,
        ssh_port=ssh_port,
        caption_port=caption_port,
        image_url=IMAGE_URL,
        image_sha256=IMAGE_SHA256,
    )
    validate_spec(values)
    root = private_directory(root)
    regular_files(root)
    with (root / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = root / "owner.json"
        if marker.exists():
            old = validate_spec(json.loads(marker.read_text()))
            values["id"] = old["id"]
            if old != values:
                raise ValueError("Retained VM settings differ; export a new VM storage directory")
        else:
            if set(p.name for p in root.iterdir()) != {"runner.lock"}:
                raise ValueError("Refusing to adopt nonempty VM storage without an owner")
            atomic_json(marker, values)
        credentials = root / "credentials"
        credentials.mkdir(exist_ok=True, mode=0o700)
        for name in ("operator", "admin", "host"):
            key = credentials / name
            if not key.exists():
                subprocess.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "ed25519",
                        "-N",
                        "",
                        "-C",
                        "live-transcript-managed",
                        "-f",
                        str(key),
                    ],
                    check=True,
                    timeout=15,
                )
            key.chmod(0o600)
            # Detect an incomplete/replaced pair; never silently rotate guest trust.
            public = subprocess.check_output(["ssh-keygen", "-y", "-f", str(key)], timeout=10)
            if public.split()[:2] != key.with_suffix(".pub").read_bytes().split()[:2]:
                raise ValueError("VM key pair does not match")
        public = (credentials / "host.pub").read_text().split()
        atomic_text(
            credentials / "known_hosts",
            "\n".join(f"[127.0.0.1]:{p} " + " ".join(public[:2]) for p in sorted({ssh_port, 2222}))
            + "\n",
        )
    return values


def ssh(root, command, *, timeout=12, input=None, log=None, admin=False):
    return (
        subprocess.run(
            [
                "ssh",
                "-i",
                str(root / ("credentials/admin" if admin else "credentials/operator")),
                "-p",
                "2222",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ConnectTimeout=4",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(root / "credentials/known_hosts"),
                "root@127.0.0.1" if admin else "transcript@127.0.0.1",
                command,
            ],
            stdout=log or subprocess.DEVNULL,
            stderr=log or subprocess.DEVNULL,
            timeout=timeout,
            input=input,
        ).returncode
        == 0
    )


def guest_ready(root, spec):
    # Require the post-reboot kernel/media devices, not just cloud-init's marker.
    try:
        return ssh(
            root,
            'test "$(cat /var/lib/live-transcript/ready 2>/dev/null)" = '
            + spec["id"]
            + " && test -c /dev/video0 && grep -q '^snd_dummy ' /proc/modules"
            + " && /opt/live-transcript/venv/bin/python -c 'import playwright.sync_api,PIL'",
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def verify_media(root, guest_config):
    """First-boot smoke check only; the guest lease refuses to touch a live call."""
    from live_transcript.runtime.deployment import build_bundle

    bundle = root / "bootstrap.pyz"
    build_bundle(bundle)
    with (root / "bootstrap.log").open("ab") as log:
        if not ssh(
            root, "cat > /home/transcript/live-transcript.pyz", input=bundle.read_bytes(), log=log
        ):
            return False
        if not ssh(
            root,
            "mkdir -p /home/transcript/.config/live-transcript && cat > /home/transcript/.config/live-transcript/config.toml",
            input=Path(guest_config).read_bytes(),
            log=log,
        ):
            return False
        return ssh(
            root,
            "export LIVE_TRANSCRIPT_CONFIG=/home/transcript/.config/live-transcript/config.toml; "
            "exec xvfb-run -a -s '-screen 0 1280x960x24' /opt/live-transcript/venv/bin/python -u /home/transcript/live-transcript.pyz check-media",
            timeout=150,
            log=log,
        )


def retry_setup(root, spec):
    """Explicit repair of a failed first boot, never an initialized/live guest."""
    if (root / "initialized.json").exists():
        raise ValueError("Guest was already initialized; setup repair is refused")
    if json.loads((root / "owner.json").read_text()) != spec:
        raise ValueError("VM owner/specification mismatch")
    from importlib.resources import files

    # Hold the same guest lease used by meetings. cloud-final must have exited.
    guard = (
        "set -e\n"
        "test -f /var/lib/live-transcript/failed\n"
        "test ! -f /var/lib/live-transcript/ready\n"
        'test "$(systemctl show cloud-final.service -p MainPID --value)" = 0\n'
        'test -z "$(runuser -u transcript -- env XDG_RUNTIME_DIR=/run/user/$(id -u transcript) '
        "systemctl --user list-units --no-legend --state=active,activating,deactivating 'live-transcript-meeting-*')\"\n"
    )
    script = files("live_transcript.runtime").joinpath("guest-bootstrap.sh").read_bytes()
    with (root / "repair.lock").open("a") as lock, (root / "bootstrap.log").open("ab") as log:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = (
            "install -d -m 0700 -o transcript -g transcript /home/transcript/logs && "
            "touch /home/transcript/logs/meeting-owner.lock && chown transcript:transcript /home/transcript/logs/meeting-owner.lock && "
            "flock -n /home/transcript/logs/meeting-owner.lock bash -s -- " + spec["id"]
        )
        if not ssh(root, command, input=guard.encode() + script, timeout=1800, admin=True, log=log):
            raise RuntimeError("First-boot repair failed/refused; inspect bootstrap.log")
        if not ssh(root, "shutdown -r +1", admin=True, log=log):
            raise RuntimeError("Repair finished but reboot was not scheduled")


def initialize_disk(root, spec):
    """Only publish complete initial files. Retained system disks are immutable here."""
    disk = root / "system.qcow2"
    if disk.exists():
        if not (root / "seed.iso").is_file():
            raise ValueError("Retained VM disk has no seed; refusing to regenerate identity")
        return
    base = download(spec["image_url"], root / "base.img", spec["image_sha256"])
    credentials = root / "credentials"
    atomic_text(
        root / "user-data",
        cloud_config(
            (credentials / "operator.pub").read_text(),
            (credentials / "host").read_text(),
            (credentials / "host.pub").read_text(),
            spec["id"],
            (credentials / "admin.pub").read_text(),
            gateway_alias=True,
        ),
    )
    atomic_json(
        root / "meta-data", {"instance-id": spec["id"], "local-hostname": "live-transcript"}
    )
    temporary = root / "seed.incoming.iso"
    temporary.unlink(missing_ok=True)
    subprocess.run(
        ["cloud-localds", str(temporary), str(root / "user-data"), str(root / "meta-data")],
        check=True,
        timeout=30,
    )
    os.replace(temporary, root / "seed.iso")
    temporary = root / "system.incoming.qcow2"
    temporary.unlink(missing_ok=True)
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(base),
            str(temporary),
            str(spec["disk_gib"]) + "G",
        ],
        check=True,
        timeout=60,
    )
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, disk)


def qemu_command(root, spec):
    return [
        "qemu-system-x86_64",
        "-no-user-config",
        "-nodefaults",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-m",
        str(spec["memory_mib"]),
        "-smp",
        str(spec["vcpus"]),
        "-uuid",
        spec["id"],
        "-display",
        "none",
        "-monitor",
        "none",
        "-drive",
        f"if=none,id=system,file={root}/system.qcow2,format=qcow2,cache=none",
        "-device",
        "virtio-blk-pci,drive=system",
        "-device",
        "virtio-scsi-pci,id=scsi",
        "-drive",
        f"if=none,id=seed,file={root}/seed.iso,format=raw,readonly=on",
        "-device",
        "scsi-cd,drive=seed",
        "-netdev",
        "user,id=net,hostfwd=tcp:0.0.0.0:2222-:22",
        "-device",
        "virtio-net-pci,netdev=net",
        "-device",
        "virtio-rng-pci",
        "-chardev",
        f"file,id=serial,path={root}/serial.log,append=on",
        "-serial",
        "chardev:serial",
        "-qmp",
        f"unix:{root}/qmp.sock,server=on,wait=off",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
    ]


def qmp(root, command):
    if command not in ("query-status", "system_powerdown", "quit"):
        raise ValueError("Unsupported QMP command")
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(3)
        conn.connect(str(root / "qmp.sock"))
        with conn.makefile("rwb", buffering=0) as stream:

            def response():
                for _ in range(32):
                    line = stream.readline(65537)
                    if len(line) > 65536 or not line:
                        raise ValueError("Invalid QMP response")
                    value = json.loads(line)
                    if "event" not in value:
                        return value
                raise ValueError("Too many QMP events")

            if "QMP" not in response():
                raise ValueError("Missing QMP greeting")
            for name in ("qmp_capabilities", command):
                stream.write(json.dumps({"execute": name}).encode() + b"\n")
                result = response()
                if "return" not in result:
                    raise ValueError("QMP command failed")
            return result["return"]


class CaptionRelay(socketserver.ThreadingTCPServer):
    """Fixed-target relay accessible only through QEMU's local guest gateway."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", port), CaptionConnection)

    def verify_request(self, request, client_address):
        # Acquire before creating threads, so a broken guest cannot exhaust them.
        return self.slots.acquire(blocking=False)


class CaptionConnection(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(("captions", 8099), timeout=5) as remote:
                self.request.settimeout(5)
                remote.settimeout(5)
                while True:
                    ready, _, _ = select.select([self.request, remote], [], [], 60)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (remote if source is self.request else self.request).sendall(data)
        except OSError:
            pass  # guest's durable transport owns retry; never acknowledge lost data
        finally:
            self.server.slots.release()


def serve(root, spec, *, stop=None, guest_config="/config/guest.toml"):
    root = private_directory(root)
    regular_files(root)
    validate_spec(spec)
    if json.loads((root / "owner.json").read_text()) != spec:
        raise ValueError("VM owner/specification mismatch")
    stop = stop or threading.Event()
    with (root / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A second runner is excluded before touching disk, readiness or QMP.
        initialize_disk(root, spec)
        (root / "qmp.sock").unlink(missing_ok=True)
        (root / "health.json").unlink(missing_ok=True)
        relay = CaptionRelay(spec["caption_port"])
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        with (root / "qemu.log").open("ab") as log:
            process = subprocess.Popen(qemu_command(root, spec), stdout=log, stderr=log)
            try:
                print("Guest starting; first boot installs browser and media tools", flush=True)
                while process.poll() is None and not stop.wait(
                    30 if (root / "initialized.json").exists() else 5
                ):
                    ready = guest_ready(root, spec)
                    if ready and not (root / "initialized.json").exists():
                        try:
                            ready = verify_media(root, guest_config)
                        except (OSError, ValueError, subprocess.SubprocessError):
                            ready = False
                        if not ready:
                            print("Initial media check pending; inspect bootstrap.log", flush=True)
                            stop.wait(30)
                    atomic_json(root / "health.json", dict(ready=ready, checked=time.time()))
                    if ready and not (root / "initialized.json").exists():
                        atomic_json(
                            root / "initialized.json", dict(id=spec["id"], ready=time.time())
                        )
                        print("Guest ready", flush=True)
                if process.poll() is not None and not stop.is_set():
                    raise RuntimeError("QEMU exited; inspect persistent qemu.log and serial.log")
            finally:
                (root / "health.json").unlink(missing_ok=True)
                relay.shutdown()
                relay.server_close()
                if process.poll() is None:
                    print("Requesting guest shutdown; allowing recorder cleanup", flush=True)
                    try:
                        qmp(root, "system_powerdown")
                        process.wait(timeout=120)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                (root / "qmp.sock").unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/vm")
    parser.add_argument("--spec", default="/config/vm.json")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--retry-setup", action="store_true")
    args = parser.parse_args()
    root = Path(args.root)
    if args.health:
        try:
            value = json.loads((root / "health.json").read_text())
            return 0 if value["ready"] and 0 <= time.time() - value["checked"] < 60 else 1
        except (OSError, ValueError, KeyError):
            return 1
    spec = json.loads(Path(args.spec).read_text())
    if args.retry_setup:
        retry_setup(root, validate_spec(spec))
        return 0
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    serve(root, spec, stop=stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
