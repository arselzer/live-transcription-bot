"""Optional Compose supervision for CPU speech services; browsers stay in VMs."""

from importlib.resources import files
import hashlib
import json
from pathlib import Path
import time
import urllib.request

from live_transcript.core.files import atomic_json, atomic_text


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
