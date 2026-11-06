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
        "name": "transcript",
        "home": "/home/transcript",
        "ip": "",
        "manage_libvirt": True,
    },
    "audio": {"sink": "transcript"},
    "google": {"input_mode": "native", "admission_timeout": 330.0},
    "browser": {"google_mode": "cdp", "google_executable": "", "google_private": True},
    "caption": {
        "host": "127.0.0.1",
        "bind": "127.0.0.1",
        "port": 8099,
        "stt_url": "http://127.0.0.1:9091/inference",
        "silence_rms": 40.0,
        "run_every": 1.5,
        "silence_finalize": 3.5,
        "commit_once": True,
    },
    "asr": {
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
    "model": {"url": "http://127.0.0.1:8078/v1/chat/completions", "name": "local-model"},
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
    vm = config["vm"]
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
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", config["audio"]["sink"]):
        raise ValueError("audio.sink must be a simple PulseAudio sink name")
    if config["google"]["input_mode"] not in ("native", "dom"):
        raise ValueError("google.input_mode must be native or dom")
    if not 1 <= config["google"]["admission_timeout"] <= 600:
        raise ValueError("google.admission_timeout must be between 1 and 600 seconds")
    if config["browser"]["google_mode"] not in ("cdp", "playwright"):
        raise ValueError("browser.google_mode must be cdp or playwright")
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
        "LT_CAPTION_HOST": get("caption", "host"),
        "LT_CAPTION_PORT": str(get("caption", "port")),
        "LT_SAFETY": get("paths", "safety_copies"),
        "LT_POSTPROCESS_PYTHON": get("paths", "postprocess_python"),
        "LT_MANAGE_LIBVIRT": "1" if get("vm", "manage_libvirt") else "0",
        "LT_VAULT": get("paths", "vault"),
    }
    return "\n".join(key + "=" + shlex.quote(value) for key, value in mapping.items())


def write_vm_config(destination):
    """Deploy only guest-relevant settings; host paths and calendar remain private."""
    home = get("vm", "home")
    sections = {
        "caption": CONFIG["caption"],
        "audio": CONFIG["audio"],
        "browser": CONFIG["browser"],
        "google": CONFIG["google"],
        "recovery": CONFIG["recovery"],
        "admission": CONFIG["admission"],
        "identity": CONFIG["identity"],
        "model": CONFIG["model"],
        "paths": {"root": home},
        "vm": {"home": home},
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
