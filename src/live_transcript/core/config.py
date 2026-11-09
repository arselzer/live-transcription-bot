"""Shared configuration for the standalone tool, services and optional Hermes hooks.

Precedence: built-in defaults < TOML file selected by LIVE_TRANSCRIPT_CONFIG.
Existing CAPTION_* environment overrides remain supported by caption services.
Configuration contains deployment settings, not meeting passwords or transcripts.
"""

import copy
import json
import os
import re
from pathlib import Path
import shlex
import sys
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from live_transcript.core.files import atomic_text

ROOT = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "live-transcript"
DEFAULTS = {
    "paths": {
        "root": str(ROOT),
        "state": str(ROOT / "state"),
        "logs": str(ROOT / "logs"),
        "recordings": str(ROOT / "recordings"),
        "archive_destination": str(ROOT / "recordings"),
        "safety_copies": str(ROOT / "safety-copies"),
        "whisper_cli": str(Path.home() / "whisper.cpp/build/bin/whisper-cli"),
        "model": str(ROOT / "models/whisper-local/ggml-large-v3.bin"),
        "small_model": str(ROOT / "models/whisper-local/ggml-small.bin"),
        "cpu_model": str(ROOT / "models/faster-whisper-large-v3"),
        "glossary": "",
        "tts": str(ROOT / "tts"),
        "diarization_model": str(ROOT / "models/ecapa"),
        "whisper_server": str(Path.home() / "whisper.cpp/build/bin/whisper-server"),
        "vad": str(ROOT / "models/whisper-local/ggml-silero-v6.2.0.bin"),
        "postprocess_python": sys.executable,
        "vault": "",
        "archive_mount": "",
    },
    "postprocess": {
        "chunk_seconds": 300,
        "retry_chunk_seconds": 30,
        "timeout_seconds": 1200,
        "diarization_enabled": False,
    },
    "vm": {
        "ssh": "",
        "port": 22,
        "name": "transcript",
        "home": "/home/transcript",
        "ip": "",
        "manage_libvirt": True,
        "identity_file": "",
        "known_hosts": "",
        "python": "python3",
    },
    "services": {
        "captions": "captiond.service",
        "primary": "whisper-stt.service",
        "cpu_large": "whisper-stt-cpu.service",
        "cpu_small": "whisper-stt-cpu-small.service",
    },
    "runtime": {
        "supervisor": "systemd",
        "max_jobs": 8,
        "watchdog_seconds": 30,
        "calendar_seconds": 0,
        "job_timeout_seconds": 43200,
        "job_attempts": 6,
    },
    "audio": {
        "sink": "transcript",
        "microphone_device": "hw:0,0",
        "microphone_source": "alsa_input.hw_0_0",
        "extra_startup_file": "",
    },
    "google": {
        "input_mode": "native",
        "admission_timeout": 330.0,
        "browser_media_hooks": True,
        "manual_join": False,
        "operator_recovery_seconds": 120.0,
    },
    "browser": {
        "google_mode": "cdp",
        "google_executable": "",
        "google_private": True,
        "jitsi_debug_port": 9222,
    },
    "providers": {"jitsi_hosts": ""},
    "capacity": {
        "recording_bytes_per_minute": 33554432,
        "host_copy_factor": 2,
        "min_memory_available_bytes": 536870912,
        "guest_free_reserve_bytes": 1073741824,
        "startup_primary_mib": 4096,
        "startup_cpu_small_mib": 1536,
        "startup_app_mib": 4096,
        "startup_captions_mib": 512,
        "startup_workspace_mib": 768,
        "startup_vm_overhead_mib": 512,
        "startup_reserve_mib": 512,
    },
    "console": {"login_mode": "isolated", "shared_login_file": "", "session_seconds": 7200},
    "caption": {
        "host": "127.0.0.1",
        "guest_host": "",
        "bind": "127.0.0.1",
        "port": 8099,
        "stt_url": "http://127.0.0.1:9091/inference",
        "silence_rms": 40.0,
        "run_every": 1.5,
        "silence_finalize": 3.5,
        "mixed_language_gap": 1.0,
        "commit_once": True,
    },
    "asr": {
        "primary_device": "gpu",
        "cpu_fallback": True,
        "adaptive_model": True,
        "cpu_url": "http://127.0.0.1:9092/inference",
        "small_cpu_url": "http://127.0.0.1:9093/inference",
        "gpu_timeout": 12.0,
        "cpu_timeout": 120.0,
        "gpu_retry_seconds": 30.0,
        "backlog_seconds": 20.0,
        "slow_rtf": 1.2,
        "slow_samples": 2,
        "cpu_threads": 16,
        "gpu_device": "0",
        "metrics_device": "card0",
    },
    "model": {
        "enabled": True,
        "url": "http://127.0.0.1:8078/v1/chat/completions",
        "name": "local-model",
    },
    "recovery": {
        "enabled": True,
        "model_enabled": True,
        "check_seconds": 3.0,
        "cooldown_seconds": 25.0,
        "missing_seconds": 12.0,
        "max_actions_5min": 6,
    },
    "storage": {"safety_max_bytes": 8 * 1024**3, "host_free_reserve_bytes": 16 * 1024**3},
    "calendar": {"url_file": str(Path.home() / ".config/meetbot/calendar_url"), "timezone": "UTC"},
    "identity": {"name": "Live Transcript · Recording"},
    "admission": {
        "test_min_interval_seconds": 180.0,
        "test_window_seconds": 600.0,
        "test_max_attempts": 6,
        "zoom_name_fallbacks": [],
        "name_retry_delay_seconds": 5.0,
    },
}


def load(path=None):
    config = copy.deepcopy(DEFAULTS)
    selected = Path(
        path
        or os.environ.get("LIVE_TRANSCRIPT_CONFIG")
        or Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "live-transcript/config.toml"
    ).expanduser()
    if (path is not None or os.environ.get("LIVE_TRANSCRIPT_CONFIG")) and not selected.is_file():
        raise FileNotFoundError("selected configuration does not exist: " + str(selected))
    if selected.exists():
        with selected.open("rb") as stream:
            values = tomllib.load(stream)
        for section, entries in values.items():
            if section not in config or not isinstance(entries, dict):
                raise ValueError("unknown configuration section: " + section)
            for key, value in entries.items():
                if key not in config[section]:
                    raise ValueError("unknown configuration key: " + section + "." + key)
                expected = config[section][key]
                if type(expected) is bool:
                    valid = type(value) is bool
                elif type(expected) in (int, float):
                    valid = type(value) in (int, float)
                elif type(expected) is list:
                    valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
                else:
                    valid = isinstance(value, str)
                if not valid:
                    raise ValueError("wrong configuration type: " + section + "." + key)
                config[section][key] = value
        if "root" in values.get("paths", {}):
            base = Path(config["paths"]["root"]).expanduser()
            for key, suffix in {
                "state": "state",
                "tts": "tts",
                "logs": "logs",
                "recordings": "recordings",
                "archive_destination": "recordings",
                "safety_copies": "safety-copies",
                "model": "models/whisper-local/ggml-large-v3.bin",
                "small_model": "models/whisper-local/ggml-small.bin",
                "cpu_model": "models/faster-whisper-large-v3",
                "diarization_model": "models/ecapa",
                "vad": "models/whisper-local/ggml-silero-v6.2.0.bin",
            }.items():
                if key not in values["paths"]:
                    config["paths"][key] = str(base / suffix)
    for key, value in config["paths"].items():
        if value:
            config["paths"][key] = os.path.abspath(os.path.expandvars(os.path.expanduser(value)))
    for key, lower, upper in (
        ("recording_bytes_per_minute", 1048576, 1073741824),
        ("host_copy_factor", 1, 4),
        ("min_memory_available_bytes", 0, 137438953472),
        ("guest_free_reserve_bytes", 0, 1099511627776),
    ):
        if (
            type(config["capacity"][key]) is not int
            or not lower <= config["capacity"][key] <= upper
        ):
            raise ValueError("Invalid capacity." + key)
    for key, value in config["capacity"].items():
        if key.startswith("startup_") and (type(value) is not int or not 0 <= value <= 131072):
            raise ValueError("Invalid capacity." + key)
    console = config["console"]
    if console["login_mode"] not in ("isolated", "shared"):
        raise ValueError("console.login_mode must be isolated or shared")
    if type(console["session_seconds"]) is not int or not 60 <= console["session_seconds"] <= 86400:
        raise ValueError("console.session_seconds must be an integer between 60 and 86400")
    if console["shared_login_file"]:
        console["shared_login_file"] = os.path.abspath(
            os.path.expandvars(os.path.expanduser(console["shared_login_file"]))
        )
        if any(ord(c) < 32 for c in console["shared_login_file"]):
            raise ValueError("invalid console.shared_login_file")
    if console["login_mode"] == "shared" and not console["shared_login_file"]:
        raise ValueError("console.shared_login_file is required for shared login")
    vm = config["vm"]
    if type(vm["port"]) is not int or not 1 <= vm["port"] <= 65535:
        raise ValueError("vm.port must be between 1 and 65535")
    runtime = config["runtime"]
    if runtime["supervisor"] not in ("systemd", "app"):
        raise ValueError("runtime.supervisor must be systemd or app")
    if (
        type(runtime["job_timeout_seconds"]) is not int
        or not 300 <= runtime["job_timeout_seconds"] <= 86400
        or type(runtime["job_attempts"]) is not int
        or not 1 <= runtime["job_attempts"] <= 20
    ):
        raise ValueError("Invalid application job retry/time budget")
    if (
        type(runtime["max_jobs"]) is not int
        or not 6 <= runtime["max_jobs"] <= 64
        or any(
            type(runtime[key]) is not int
            or runtime[key] not in (0,)
            and not 10 <= runtime[key] <= 86400
            for key in ("watchdog_seconds", "calendar_seconds")
        )
    ):
        raise ValueError("Invalid application supervision limits")
    if runtime["supervisor"] == "app" and (
        vm["manage_libvirt"] or any(config["services"].values())
    ):
        raise ValueError(
            "App supervision needs a prepared VM and externally supervised speech/captions; set vm.manage_libvirt=false and empty services names"
        )
    if not vm["python"] or vm["python"].startswith("-") or any(ord(c) < 32 for c in vm["python"]):
        raise ValueError("invalid VM python executable")
    for key in ("identity_file", "known_hosts"):
        if vm[key]:
            vm[key] = os.path.abspath(os.path.expandvars(os.path.expanduser(vm[key])))
            if any(ord(c) < 32 for c in vm[key]):
                raise ValueError("invalid VM " + key)
    for value in config["services"].values():
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\.service", value):
            raise ValueError("invalid service unit name")
    if config["asr"]["primary_device"] not in ("gpu", "cpu"):
        raise ValueError("asr.primary_device must be gpu or cpu")
    for key in ("ssh", "name", "ip"):
        value = vm[key]
        if value.startswith("-") or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("invalid VM " + key)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", vm["home"]):
        raise ValueError(
            "VM home must be an absolute path using letters, digits, dot, dash, underscore and slash"
        )
    if ".." in Path(vm["home"]).parts or Path(vm["home"]) == Path("/"):
        raise ValueError("VM home must be a non-root directory without parent traversal")
    try:
        ZoneInfo(config["calendar"]["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("invalid calendar.timezone") from exc
    if not re.fullmatch(
        r"hw:[0-9]{1,2},[0-9]{1,2}", config["audio"]["microphone_device"]
    ) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", config["audio"]["microphone_source"]):
        raise ValueError("Invalid audio microphone device/source")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", config["audio"]["sink"]):
        raise ValueError("audio.sink must be a simple PulseAudio sink name")
    if config["google"]["input_mode"] not in ("native", "dom"):
        raise ValueError("google.input_mode must be native or dom")
    if not 1 <= config["google"]["admission_timeout"] <= 600:
        raise ValueError("google.admission_timeout must be between 1 and 600 seconds")
    if not 0 <= config["google"]["operator_recovery_seconds"] <= 600:
        raise ValueError("google.operator_recovery_seconds must be between 0 and 600 seconds")
    if config["browser"]["google_mode"] not in ("cdp", "playwright"):
        raise ValueError("browser.google_mode must be cdp or playwright")
    port = config["browser"]["jitsi_debug_port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("browser.jitsi_debug_port must be between 1 and 65535")
    executable = config["browser"]["google_executable"]
    if executable and (not executable.startswith("/") or any(ord(c) < 32 for c in executable)):
        raise ValueError("browser.google_executable must be an absolute guest path")
    port = config["caption"]["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("caption.port must be an integer between 1 and 65535")
    name = config["identity"]["name"]
    if not name.strip() or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise ValueError("identity.name must contain 1–64 characters without control characters")
    for key in ("run_every", "silence_finalize", "silence_rms"):
        if not 0 < config["caption"][key] <= 32768:
            raise ValueError("invalid caption." + key)
    if not 0.1 <= config["caption"]["mixed_language_gap"] <= 5:
        raise ValueError("caption.mixed_language_gap must be between 0.1 and 5 seconds")
    for key in ("check_seconds", "cooldown_seconds", "missing_seconds", "max_actions_5min"):
        if not 0 < config["recovery"][key] <= 3600:
            raise ValueError("invalid recovery." + key)
    if type(config["recovery"]["max_actions_5min"]) is not int:
        raise ValueError("recovery.max_actions_5min must be an integer")
    for key in (
        "gpu_timeout",
        "cpu_timeout",
        "gpu_retry_seconds",
        "backlog_seconds",
        "slow_rtf",
        "slow_samples",
        "cpu_threads",
    ):
        if not 0 < config["asr"][key] <= 3600:
            raise ValueError("invalid asr." + key)
    for key in ("cpu_threads", "slow_samples"):
        if type(config["asr"][key]) is not int:
            raise ValueError("asr." + key + " must be an integer")
    post = config["postprocess"]
    if not (
        type(post["chunk_seconds"]) is int
        and type(post["retry_chunk_seconds"]) is int
        and 10 <= post["retry_chunk_seconds"] <= post["chunk_seconds"] <= 1800
        and type(post["timeout_seconds"]) is int
        and 1 <= post["timeout_seconds"] <= 7200
    ):
        raise ValueError("invalid postprocess chunk or timeout settings")
    for key in (
        "test_min_interval_seconds",
        "test_window_seconds",
        "test_max_attempts",
        "name_retry_delay_seconds",
    ):
        value = config["admission"][key]
        if not 0 < value <= 86400:
            raise ValueError("invalid admission." + key)
    if type(config["admission"]["test_max_attempts"]) is not int:
        raise ValueError("admission.test_max_attempts must be an integer")
    fallbacks = config["admission"]["zoom_name_fallbacks"]
    if len(fallbacks) > 2 or any(
        not name.strip() or len(name) > 64 or any(ord(c) < 32 for c in name) for name in fallbacks
    ):
        raise ValueError(
            "admission.zoom_name_fallbacks allows at most two names of 1–64 characters"
        )
    config["_file"] = str(selected.resolve())
    return config


CONFIG = load()


def get(section, key):
    return CONFIG[section][key]


def ssh_options():
    """Use installation-scoped credentials consistently for control and copies."""
    options = ["-o", "BatchMode=yes"]
    if get("vm", "port") != 22:
        options += ["-o", "Port=" + str(get("vm", "port"))]
    if get("vm", "identity_file"):
        options += ["-i", get("vm", "identity_file"), "-o", "IdentitiesOnly=yes"]
    if get("vm", "known_hosts"):
        options += [
            "-o",
            "UserKnownHostsFile=" + get("vm", "known_hosts"),
            "-o",
            "StrictHostKeyChecking=yes",
        ]
    return options


def ssh_transport():
    return shlex.join(
        [
            "ssh",
            *ssh_options(),
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
        ]
    )


def shell_values():
    mapping = {
        "LIVE_TRANSCRIPT_CONFIG": CONFIG["_file"],
        "LT_ROOT": get("paths", "root"),
        "LT_LOGS": get("paths", "logs"),
        "LT_RECORDINGS": get("paths", "recordings"),
        "LT_VM": get("vm", "ssh"),
        "LT_VM_NAME": get("vm", "name"),
        "LT_VM_HOME": get("vm", "home"),
        "LT_VM_IP": get("vm", "ip"),
        "LT_VM_PYTHON": get("vm", "python"),
        "LT_SSH_IDENTITY": get("vm", "identity_file"),
        "LT_SSH_KNOWN_HOSTS": get("vm", "known_hosts"),
        "LT_SSH_PORT": str(get("vm", "port")),
        "LT_CAPTION_UNIT": get("services", "captions"),
        "LT_CAPTION_HOST": get("caption", "host"),
        "LT_CAPTION_PORT": str(get("caption", "port")),
        "LT_SAFETY": get("paths", "safety_copies"),
        "LT_POSTPROCESS_PYTHON": get("paths", "postprocess_python"),
        "LT_MANAGE_LIBVIRT": "1" if get("vm", "manage_libvirt") else "0",
        "LT_SUPERVISOR": get("runtime", "supervisor"),
        "LT_VAULT": get("paths", "vault"),
    }
    return "\n".join(key + "=" + shlex.quote(value) for key, value in mapping.items())


def write_vm_config(destination):
    """Deploy only guest-relevant settings; host paths and calendar remain private."""
    home = get("vm", "home")
    sections = {
        "caption": dict(
            CONFIG["caption"], host=get("caption", "guest_host") or get("caption", "host")
        ),
        "audio": CONFIG["audio"],
        "browser": CONFIG["browser"],
        "google": CONFIG["google"],
        "recovery": CONFIG["recovery"],
        "admission": CONFIG["admission"],
        "identity": CONFIG["identity"],
        "providers": CONFIG["providers"],
        "model": CONFIG["model"],
        "paths": {"root": home},
        "vm": {"home": home, "python": get("vm", "python")},
    }
    text = "\n".join(
        "["
        + section
        + "]\n"
        + "\n".join(
            key + " = " + (str(value).lower() if type(value) is bool else json.dumps(value))
            for key, value in entries.items()
        )
        for section, entries in sections.items()
    )
    path = Path(destination)
    atomic_text(path, text + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--shell"]:
        print(shell_values())
    elif sys.argv[1:2] == ["--vm-config"] and len(sys.argv) == 3:
        write_vm_config(sys.argv[2])
    else:
        print(json.dumps(CONFIG, indent=2))
