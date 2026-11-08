# Container caption and speech backend

Use this when you already have a prepared browser VM and want the caption daemon
and CPU recognition to run under Docker Compose. It packages their Python, SQLite
and native speech dependencies. Neither apt nor a systemd user manager is needed
on the **backend machine**. Building the speech image uses apt inside its pinned
Debian image, independently of the host distribution.

The browser, camera, original audio/video recording and guest supervision remain
inside the VM. The host CLI still controls that VM over SSH; its meeting launcher,
retrieval jobs and final processing retain their Linux/systemd/native-tool
requirements. This is a portable backend, not a replacement for VM provisioning
or a fully containerized controller.

## Export and start

Install the Python package normally; do not run the managed `setup` command just
to export a backend. Choose an empty output directory and persistent local state
and model directories. In this example `192.0.2.1` is a placeholder for the
host's **trusted VM bridge address**; replace it with the actual address.

```bash
python3 -m venv .venv
.venv/bin/pip install .
source .venv/bin/activate
live-transcript backend "$HOME/.local/share/live-transcript/backend-v1" \
  --models "$HOME/.local/share/live-transcript/models/whisper-local" \
  --state "$HOME/.local/share/live-transcript/state" \
  --bind 192.0.2.1 --port 8099 --download-models

cd "$HOME/.local/share/live-transcript/backend-v1"
docker compose -f compose.json build
docker compose -f compose.json up -d --wait --wait-timeout 300
docker compose -f compose.json ps
docker compose -f compose.json logs --tail 100 captions
```

Without `--download-models`, export verifies existing weights against the pinned
checksums. Full, small and VAD models are required. The full model is preferred;
the small model provides degraded operation when recognition cannot keep up.
`--threads N` sets the CPU threads and quota for each speech service. Allow at
least 16 GiB RAM for the backend and VM together and sufficient local disk.

Export creates an application bundle from the installed, reviewed Python package,
two Dockerfiles, backend-only TOML and a Compose project. It does not copy your
host profile, SSH credentials, browser profile or recordings, start services,
install Docker, or change an existing deployment. The build context excludes
everything except the bundle and Dockerfiles. Rerunning into a nonempty directory
fails; export a new version into a new directory to prepare an upgrade.

Docker Engine with Compose v2 is required. Linux x86-64 Docker Engine is the
tested backend. Other architectures, Docker Desktop networking and rootless UID
mapping need validation on the target installation. Rootful Docker runs the
caption process as the exporting user's numeric UID/GID so it can write an
owner-only bind-mounted journal. Rootless Docker maps these IDs differently;
adjust the generated caption `user` and state ownership for that mapping before
starting. Do not solve a permission error by making the journal world-writable.
SELinux hosts may also require an appropriate bind-mount label.

## Connect a host profile and VM

Configure the controlling host profile to use the same caption address/port and
state directory. Empty service names prevent the CLI from trying to start native
units in addition to the containers:

```toml
[paths]
state = "/path/to/the/same/local/state"

[caption]
host = "192.0.2.1"
port = 8099

[services]
captions = ""
primary = ""
cpu_large = ""
cpu_small = ""
```

These are overrides for a complete VM profile, not a complete profile themselves.
Keep the existing `[vm]`, storage and final-transcription settings. The normal
`start` command deploys the host's current guest configuration. Test connectivity
from the VM before relying on live captions. The default backend bind is loopback,
which is useful for local checks but unreachable from an ordinary VM.

Only caption TCP is published. Speech endpoints are reachable only within the
Compose network. The caption protocol assumes a trusted network: restrict the
bridge listener to the VM network using the host firewall. Export rejects a
wildcard bind. There are no Docker socket, host systemd socket, media-device or
privileged mounts.

The workspace reads the journal locally. A backend on another machine can serve
the VM, but displaying its live transcript on the controller requires a local
journal or a future authenticated read API. Do not put SQLite/WAL on a network
filesystem. Final transcription still needs the controller's configured native
speech binary and model; the internal container endpoints do not provide that CLI.

## Notes, health and recovery

Meeting notes are disabled by default in this bundle. To enable them, export with
`--model-url https://model.example/v1/chat/completions --model-name local-model`.
The endpoint must be reachable **from the container**; `127.0.0.1` there refers
to the container itself. Credentials in the URL are rejected. Configure any
authentication in a trusted model gateway; don't put it in the exported source.

Caption ingest starts independently of speech readiness. It acknowledges a
packet only after committing it to the local journal, so an unavailable model
does not prevent durable ingestion. Host/VM originals remain the recovery source
when the journal reaches capacity or recognition is behind. The caption health
check exercises its protocol, not just a listening socket; speech workers have
separate model health checks. A healthy caption process does not imply accurate
or timely recognition.

Compose waits for configured health checks at startup. A later `unhealthy` state
does not itself restart a process; `restart: unless-stopped` handles process exits.
Recognition retry/backoff, bounded model fallback and retained audio remain
application responsibilities. See [Compose readiness](https://docs.docker.com/compose/how-tos/startup-order/)
and [restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/).

To restart a failed caption process, use `docker compose -f compose.json restart
captions`. Committed journal records and receipts survive process replacement;
the sender can safely replay an unacknowledged packet with the same identity.
Keep the state directory on local durable storage and back it up consistently
with SQLite's backup API. Current caption daemons take an exclusive journal
ownership lock across native and container deployments; a second decoder refuses
to start. Older deployed bundles need an update before they honor this lock.

Before upgrading, stop caption ingest, retain the state directory and its backup,
then start the new bundle against that same directory. Stop the old project's
containers first to avoid two journal owners or conflicting ports. Roll back the
image/configuration and matching state backup together if a future journal schema
changes. `docker compose down` removes this project's containers/network; the
bind-mounted state and models remain. Backend outages must not stop VM recording.
