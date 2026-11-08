# Run the application with Docker Compose

The application image contains the CLI, workspace/API, SSH and retrieval tools,
FFmpeg/fonts and the pinned CPU whisper.cpp binary used for final processing.
Caption ingest and the two live speech workers run as separate services. The
browser and independent audio/video recorder remain in a prepared VM.

The application host needs Docker Engine with Compose v2 and access to that VM.
It does not need host Python, FFmpeg, whisper.cpp, a systemd user manager, or an
apt-based distribution. Linux Docker Engine is the supported initial target;
this deployment uses host networking for reliable access to the existing VM
bridge and its console ports. Docker Desktop/rootless networking need separate
validation. Guest systemd remains part of the prepared VM.

## Build without installing Python on the host

From a clean, reviewed checkout:

```bash
git archive --format=tar HEAD | docker build \
  -f src/live_transcript/runtime/application.Dockerfile \
  -t live-transcript-app:local -
```

The archive contains committed source only. The build validates the source,
builds the Python wheel, and packages the media tools. No virtual environment,
local runtime configuration or recordings enter this build context. Version pins
and checksums live inside the build; they are not requirements on host packages.
Keep the image tag/digest with the deployment configuration when upgrading.

## Prepare an isolated deployment

Use a separate directory outside the checkout for `profile.toml`, SSH credentials,
models, runtime data and the exported Compose project. Start from a profile for
an **already prepared VM**. Set its `vm.identity_file` and `vm.known_hosts` to
files inside this directory, with pinned guest host trust. The export never
starts, adopts, reformats or provisions a VM.

The following commands assume you are in that deployment directory. `192.0.2.1`
is a documentation placeholder: replace it with a trusted host bridge IP that
the VM can reach. The example preserves the same directory path inside the
helper container so the generated bind mounts also refer to valid host paths.

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  --workdir "$PWD" --mount "type=bind,source=$PWD,target=$PWD" \
  live-transcript-app:local --config "$PWD/profile.toml" \
  deployment "$PWD/stack" --models "$PWD/models" --data "$PWD/data" \
  --bind 192.0.2.1 --port 8099 --speech-port 9091 --small-speech-port 9093 \
  --download-models

cd stack
docker compose -f compose.json build
docker compose -f compose.json up -d --wait --wait-timeout 300
docker compose -f compose.json exec -T app live-transcript --json check
```

Alternatively, an installed CLI can run the same `deployment` command directly.
Without `--download-models`, existing public weights must match their checksums.
The exporter requires a new empty output directory and never changes the source
profile. It creates a separate container profile; inspect that profile before
starting. All selected ports must be unused on the host. Restrict the caption
listener to the trusted VM network. Speech ports bind to loopback only.

The application runs under the exporting user's numeric UID/GID by default,
with a matching account inside its image for SSH. Keys are mounted read-only;
they are excluded from the build context. The data volume belongs to that user.
Rootful Docker is supported without granting the application `privileged`, media
devices, Docker socket access, or the host's systemd/cgroup sockets. Running the
application as root is unnecessary. Rootless UID mapping and SELinux volume
labels require target-specific configuration.

Allow at least 24 GiB RAM for a full CPU backend plus VM and final processing;
measure actual throughput and adjust the generated CPU/memory limits for the
machine. Recordings and SQLite state must have enough local disk space. The
services' memory ceilings are limits, not guaranteed reservations.

## Operate it

Use the CLI **inside this same application container**:

```bash
docker compose -f compose.json exec -T app live-transcript start 'INVITATION' --max-min 90
docker compose -f compose.json exec -T app live-transcript --json status
docker compose -f compose.json exec -T app live-transcript --json jobs
docker compose -f compose.json exec -T app live-transcript --json console \
  --control --listen 0.0.0.0 --advertise-host console.example --port 6080
docker compose -f compose.json exec -T app live-transcript stop
```

Open the private console URL for the light-themed transcript workspace and VM
controls. With host networking, `6080` is a host port; adding a Compose `ports`
entry does not publish or restrict it. Use loopback by default or a trusted VPN,
and terminate TLS upstream for untrusted networks.
The console still needs an existing meeting display; `console --idle --control`
creates a bounded diagnostic desktop and does not record a manual meeting.

`start`, `console`, `recover` and `process` submit durable application jobs and
follow their output. Closing that CLI reader does not own or kill the job. Final
processing and mirrors survive an application restart through their checkpoints.
`jobs` includes scheduler status alongside recording/retrieval checkpoints; an
exited job is not proof that a transcript passed quality checks.

Runtime files live under `data`: original retrievals and outputs in `recordings`,
caption journal in `state`, application queue/logs in `state/application`, and
meeting/recovery state in `logs`. VM originals remain inside the VM until a
separate retention decision. Bring containers down without deleting these bind
mounts to retain recordings and work.

## Supervision and restart behavior

The container uses a small init for signal forwarding/reaping and an application
scheduler with a durable SQLite queue. It does not run systemd inside Docker.
Each worker records its process identity and owns its child process group. A
scheduler restart adopts an existing wrapper. Container replacement resumes
bounded retryable work, and serialized final processing avoids launching many
full-model decoders at once. Interrupted meeting launches are not blindly replayed:
the session watchdog inspects the owned guest recorder before deciding recovery.

The default watchdog runs every 30 seconds. Calendar polling is disabled until
an ICS file is mounted and configured; set `runtime.calendar_seconds` to enable
it. Due calendar tasks remain durable and still revalidate the event before
launching. `runtime.job_timeout_seconds`, `job_attempts` and `max_jobs` bound
worker lifetime/retries/concurrency. At least six slots are required so recording
backups and control can coexist. Only named application tasks can be queued.

App health means its scheduler is running. Caption protocol health, ASR model
readiness, guest admission and source recording progress remain separate checks.
Docker restart policy handles process exits; health checks alone do not restart
an unhealthy process. Never restart or stop the VM as a response to a caption,
summary, application, or UI failure.

## Hermes

Configure Hermes to invoke a fixed wrapper around `docker compose -f ABSOLUTE_PATH
exec -T app live-transcript`, then use the same commands and notification contract
as the [native integration](hermes.md). Keep the Compose project and container
profile fixed in that wrapper. Do not have native cron scripts read the mounted
container state as if its PIDs belonged to the host namespace. The built-in
watchdog queues alerts but does not send messages or acknowledge delivery; Hermes
reads `notifications` and acknowledges only after successful authorized delivery.

## VM prerequisite

The Docker deployment connects to an already prepared browser VM. Use the native
`setup` command to provision one if needed. The application container does not
start, replace or provision the VM.

The standard image uses CPU speech and does not bundle optional Piper voices,
speaker-diarization models or GPU drivers. Supply those assets and runtime
configuration separately if enabling those features. The optional notes URL
must be reachable both from the app's host network and the caption container;
their loopback addresses and Compose DNS contexts differ.
