"""Owned Compose releases with reversible application updates and no VM restarts."""

from contextlib import contextmanager
import fcntl
import hashlib
from importlib.resources import files
import json
import os
import re
from pathlib import Path
import shutil
import shlex
import subprocess
import tomllib

from live_transcript.core.files import atomic_json, atomic_text

COMPATIBILITY = 1
CODE_FILES = ("application.Dockerfile", "backend.Dockerfile", "backend.pyz")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def owned(directory):
    root = Path(directory).expanduser().absolute()
    if root.resolve() != root or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("Deployment must be an owned directory without symlinks")
    return root


def regular(path):
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_nlink != 1
    ):
        raise ValueError("Deployment files must be owned regular files")
    return path


def manifest(root):
    value = json.loads(regular(root / "deployment.json").read_text())
    if (
        not isinstance(value, dict)
        or value.get("compatibility") != COMPATIBILITY
        or not isinstance(value.get("revision"), str)
        or not re.fullmatch(r"[a-f0-9]{16}", value["revision"])
    ):
        raise ValueError("Deployment state is incompatible with this release")
    names = value.get("files")
    if (
        not isinstance(names, dict)
        or not set(CODE_FILES).issubset(names)
        or len(names) != len(CODE_FILES) + 1
    ):
        raise ValueError("Invalid release artifact inventory")
    for name, checksum in names.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or "\\" in name
            or name not in CODE_FILES
            and not name.endswith(".whl")
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[a-f0-9]{64}", checksum)
        ):
            raise ValueError("Invalid release artifact")
    if not isinstance(value.get("images"), dict) or set(value["images"]) != {
        "app",
        "workspace",
        "captions",
    }:
        raise ValueError("Invalid release service inventory")
    if any(
        not isinstance(image, str) or not re.fullmatch(r"[a-z0-9][a-zA-Z0-9._:/@-]{0,250}", image)
        for image in value["images"].values()
    ):
        raise ValueError("Invalid release image identity")
    return value


def code_files(root):
    wheels = list(root.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Deployment requires exactly one application wheel")
    return [*CODE_FILES, wheels[0].name]


def snapshot(root, state):
    target = root / ".releases" / state["revision"]
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned(target)
    for name in ["compose.json", *state["files"]]:
        data = regular(root / name).read_bytes()
        destination = target / name
        if destination.exists():
            regular(destination)
        descriptor = os.open(
            destination, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
    atomic_json(target / "deployment.json", state)


def register(directory):
    root = owned(directory)
    if (root / "deployment.json").exists():
        raise ValueError("Deployment already has an owner receipt")
    names = code_files(root)
    wheel = next(root.glob("*.whl"))
    revision = digest(wheel)[:16]
    compose = json.loads((root / "compose.json").read_text())
    ident = compose["name"]
    for name in ("app", "workspace", "captions"):
        service = compose["services"][name]
        service["image"] += "-" + revision
        service.setdefault("environment", {})["LIVE_TRANSCRIPT_RELEASE"] = revision
    atomic_json(root / "compose.json", compose)
    state = dict(
        compatibility=COMPATIBILITY,
        installation=ident,
        revision=revision,
        previous=None,
        files={name: digest(root / name) for name in names},
        images={
            name: compose["services"][name]["image"] for name in ("app", "workspace", "captions")
        },
    )
    atomic_json(root / "deployment.json", state)
    snapshot(root, state)
    return state


def verify(root, state):
    if any(digest(regular(root / name)) != checksum for name, checksum in state["files"].items()):
        raise ValueError("Deployment code was edited; refusing to overwrite it")
    compose = json.loads(regular(root / "compose.json").read_text())
    if compose.get("name") != state["installation"] or any(
        compose["services"][key]["image"] != value for key, value in state["images"].items()
    ):
        raise ValueError("Deployment service identity changed")
    return compose


@contextmanager
def release_lock(root, name="release.lock"):
    descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prepare_update(root):
    from live_transcript.runtime.deployment import build_application_wheel, build_bundle
    from live_transcript.runtime.containers import write_application_dockerfile
    import tempfile

    state = manifest(root)
    if (root / "pending-release.json").exists():
        raise ValueError("An update is pending; finish or roll it back first")
    compose = verify(root, state)
    snapshot(root, state)  # include current operator tuning in the rollback copy
    with tempfile.TemporaryDirectory(prefix=".update-", dir=root) as temporary:
        stage = Path(temporary)
        wheel = build_application_wheel(stage)
        revision = digest(wheel)[:16]
        if revision == state["revision"]:
            return dict(changed=False, revision=revision)
        build_bundle(stage / "backend.pyz")
        atomic_text(
            stage / "backend.Dockerfile",
            files("live_transcript.runtime").joinpath("backend.Dockerfile").read_text(),
        )
        write_application_dockerfile(stage)
        atomic_json(
            root / "pending-release.json",
            dict(previous=state["revision"], revision=revision, compatibility=COMPATIBILITY),
        )
        # The pending receipt is durable before any build input is changed.
        for name in code_files(stage):
            with (stage / name).open("rb") as source, (root / name).open("wb") as output:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(source, output)
        for name in state["files"]:
            if name.endswith(".whl") and name != wheel.name:
                (root / name).unlink()
        for name in ("app", "workspace", "captions"):
            compose["services"][name]["image"] = (
                state["images"][name].rsplit("-", 1)[0] + "-" + revision
            )
            compose["services"][name].setdefault("environment", {})["LIVE_TRANSCRIPT_RELEASE"] = (
                revision
            )
        atomic_json(root / "compose.json", compose)
    return dict(changed=True, revision=revision)


def commit_update(root):
    old = manifest(root)
    if not (root / "pending-release.json").exists():
        verify(root, old)
        return dict(changed=False, revision=old["revision"])
    pending = json.loads(regular(root / "pending-release.json").read_text())
    if pending.get("revision") == old["revision"]:
        verify(root, old)
        snapshot(root, old)
        (root / "pending-release.json").unlink()
        return dict(revision=old["revision"])
    if pending.get("compatibility") != COMPATIBILITY or pending["previous"] != old["revision"]:
        raise ValueError("Pending update is incompatible")
    compose = json.loads(regular(root / "compose.json").read_text())
    state = dict(
        old,
        revision=pending["revision"],
        previous=old["revision"],
        files={name: digest(regular(root / name)) for name in code_files(root)},
        images={name: compose["services"][name]["image"] for name in old["images"]},
    )
    atomic_json(root / "deployment.json", state)
    snapshot(root, state)
    (root / "pending-release.json").unlink()
    return dict(revision=state["revision"])


def rollback(root):
    current = manifest(root)
    pending = root / "pending-release.json"
    previous = (
        json.loads(regular(pending).read_text())["previous"]
        if pending.exists()
        else current["previous"]
    )
    if not isinstance(previous, str) or not __import__("re").fullmatch(r"[a-f0-9]{16}", previous):
        raise ValueError("There is no compatible rollback release")
    target = owned(root / ".releases" / previous)
    state = manifest(target)
    if state["compatibility"] != current["compatibility"]:
        raise ValueError("Rollback would cross an incompatible state schema")
    verify(target, state)
    for name in ["compose.json", *state["files"]]:
        source = regular(target / name)
        destination = root / name
        if destination.is_symlink():
            raise ValueError("Deployment output is a symlink")
        with source.open("rb") as input_stream, destination.open("wb") as output:
            os.fchmod(output.fileno(), 0o600)
            shutil.copyfileobj(input_stream, output)
    for wheel in root.glob("*.whl"):
        if wheel.name not in state["files"]:
            regular(wheel).unlink()
    state["previous"] = (
        current["revision"] if previous != current["revision"] else state.get("previous")
    )
    atomic_json(root / "deployment.json", state)
    pending.unlink(missing_ok=True)
    return dict(revision=state["revision"])


def startup_capacity(root, running):
    if (root / "pending-release.json").exists():
        raise ValueError("Finish or roll back the pending update before starting services")
    from live_transcript.runtime.capacity import memory_available

    compose = json.loads(regular(root / "compose.json").read_text())
    from live_transcript.core.config import DEFAULTS

    profile = tomllib.loads(regular(root / "application.toml").read_text())
    settings = dict(DEFAULTS["capacity"], **profile.get("capacity", {}))
    reserve = profile.get("storage", {}).get(
        "host_free_reserve_bytes", DEFAULTS["storage"]["host_free_reserve_bytes"]
    )
    budgets = {
        name: settings["startup_" + name + "_mib"]
        for name in ("primary", "cpu_small", "app", "captions", "workspace")
    }
    if any(
        type(v) is not int or not 0 <= v <= 131072
        for key, v in settings.items()
        if key.startswith("startup_")
    ):
        raise ValueError("Invalid startup memory budget")
    if "vm" in compose["services"]:
        spec = json.loads(regular(root / "vm.json").read_text())
        budgets["vm"] = spec["memory_mib"] + settings["startup_vm_overhead_mib"]
        storage = next(
            v["source"] for v in compose["services"]["vm"]["volumes"] if v["target"] == "/vm"
        ).replace("$$", "$")
        if shutil.disk_usage(storage).free < reserve:
            raise RuntimeError("VM backing storage is below its configured startup reserve")
    required = sum(value for name, value in budgets.items() if name not in running) * 1024**2
    available = memory_available()
    if available is None:
        raise RuntimeError("Cannot establish host memory headroom")
    if available < required + settings["startup_reserve_mib"] * 1024**2:
        raise RuntimeError(
            "Insufficient memory headroom for stopped services; lower concurrency or free resources"
        )
    return dict(required_bytes=required, available_bytes=available)


def mounted_path(service, value):
    """Resolve a container path through its explicit bind mounts only."""
    path = Path(value)
    for volume in sorted(service["volumes"], key=lambda v: len(v["target"]), reverse=True):
        target = Path(volume["target"])
        if path.is_relative_to(target):
            return Path(volume["source"].replace("$$", "$")) / path.relative_to(target)
    raise ValueError("Path is outside the deployment mounts")


def launch_lock(root):
    compose = json.loads(regular(root / "compose.json").read_text())
    profile = tomllib.loads(regular(root / "application.toml").read_text())
    logs = profile["paths"].get("logs", profile["paths"]["root"] + "/logs")
    directory = mounted_path(compose["services"]["app"], logs)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return release_lock(directory, "run.lock")


def guard_stop(root, running):
    compose = json.loads(regular(root / "compose.json").read_text())
    service = compose["services"]["app"]
    profile = tomllib.loads(regular(root / "application.toml").read_text())
    logs = profile["paths"].get("logs", profile["paths"]["root"] + "/logs")
    state = mounted_path(service, logs) / "current_meeting.json"
    if state.exists() and json.loads(state.read_text()).get("status") in ("active", "control_lost"):
        raise RuntimeError("Stop the meeting and verify retrieval before shutting down its VM")
    if "vm" in compose["services"] and "vm" not in running:
        return dict(ok=True, vm="stopped")
    vm = profile["vm"]
    if (
        not vm["ssh"]
        or vm["ssh"].startswith("-")
        or any(c.isspace() or ord(c) < 32 for c in vm["ssh"])
    ):
        raise ValueError("Invalid configured VM destination")
    options = [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(mounted_path(service, vm["identity_file"])),
        "-o",
        "UserKnownHostsFile=" + str(mounted_path(service, vm["known_hosts"])),
        "-p",
        str(vm.get("port", 22)),
    ]
    probe = """import fcntl, pathlib, subprocess, sys
lock = (pathlib.Path(sys.argv[1]) / 'logs/meeting-owner.lock').open('a')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
r = subprocess.run(['systemctl','--user','list-units','--type=service',
    '--state=active,activating,deactivating','--no-legend',
    'live-transcript-meeting-*','live-transcript-archive-*','live-transcript-desktop-*'],
    capture_output=True, text=True, check=True, timeout=5)
if r.stdout.strip(): raise RuntimeError('Guest still owns a meeting or recorder')
for p in pathlib.Path('/proc').iterdir():
    try:
        if p.name.isdigit() and (p / 'comm').read_text().strip() == 'ffmpeg':
            raise RuntimeError('Guest media recorder is still running')
    except (FileNotFoundError, PermissionError, ProcessLookupError): pass
"""
    result = subprocess.run(
        ["ssh", *options, vm["ssh"], shlex.join([vm["python"], "-c", probe, vm["home"]])],
        capture_output=True,
        timeout=12,
    )
    if result.returncode:
        raise RuntimeError(
            "Guest recording ownership is active or unknown; inspect it before shutdown"
        )
    return dict(ok=True, vm="idle")


def run(args):
    root = owned(args.directory)
    if args.action == "hold-lock":
        import sys

        from contextlib import ExitStack

        with ExitStack() as leases:
            leases.enter_context(release_lock(root, "operation.lock"))
            if args.stop_guard:
                leases.enter_context(launch_lock(root / "stack"))
            print("LOCKED", flush=True)
            sys.stdin.buffer.read(1)
        return dict(unlocked=True)
    with release_lock(root):
        if args.action == "prepare-update":
            return prepare_update(root)
        if args.action == "commit-update":
            return commit_update(root)
        if args.action == "abort-update":
            return (
                rollback(root) if (root / "pending-release.json").exists() else dict(changed=False)
            )
        if args.action == "rollback":
            return rollback(root)
        state = manifest(root)
        if args.action == "capacity":
            return startup_capacity(root, args.running)
        if args.action == "guard-stop":
            return guard_stop(root, args.running)
        if args.action == "url":
            from live_transcript.runtime.operator import ConsoleAuth

            compose = json.loads((root / "compose.json").read_text())
            service = compose["services"]["workspace"]
            options = service["command"]
            data = next(v["source"] for v in service["volumes"] if v["target"] == "/data").replace(
                "$$", "$"
            )
            profile = tomllib.loads(regular(root / "application.toml").read_text())
            console = profile.get("console", {})
            auth_path = (
                mounted_path(service, console["shared_login_file"])
                if console.get("login_mode") == "shared"
                else Path(data) / "state/workspace-login.json"
            )
            auth = ConsoleAuth("shared", str(auth_path))
            host = options[options.index("--advertise-host") + 1]
            port = options[options.index("--port") + 1]
            from urllib.parse import urlencode

            if "--public-url" in options:
                from live_transcript.runtime.workspace import public_origin

                origin = public_origin(options[options.index("--public-url") + 1])
            else:
                origin = f"http://{host}:{port}"
            return dict(url=origin + "/#" + urlencode(dict(token=auth.token)))
        return dict(
            revision=state["revision"],
            previous=state["previous"],
            update_pending=(root / "pending-release.json").exists(),
        )
