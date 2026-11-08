# Run the application with Docker Compose

The application image contains the CLI, workspace/API, SSH and retrieval tools,
FFmpeg/fonts and the pinned CPU whisper.cpp binary used for final processing.
Caption ingest, the persistent workspace and the two live speech workers run as separate services. The
browser and independent audio/video recorder always run in a VM.

On an x86-64 Linux host with Docker Engine, Compose v2 and working `/dev/kvm`,
Compose can provision and run that VM too. Host Python, libvirt, FFmpeg, whisper.cpp,
a systemd user manager and an apt-based host distribution are unnecessary. Initial
setup needs internet access for the verified Ubuntu image, packages and models.
Allow at least 24 GiB RAM and local space for the VM, models and recordings.
Docker Desktop, rootless Docker, non-x86 hosts and SELinux volume labels need
separate validation. The guest uses systemd; the application containers do not.

## Use the deployment wrapper

From a cloned checkout:

```bash
./deploy.sh init /path/to/deployment
./deploy.sh up /path/to/deployment
./deploy.sh check /path/to/deployment
./deploy.sh workspace /path/to/deployment
./deploy.sh status /path/to/deployment
```

`init` accepts the resource, port, speech and workspace options below. Runtime
storage stays beneath its deployment directory; use the low-level export for
custom mount layouts. For example, use
`--workspace-listen 0.0.0.0 --workspace-host console.example --workspace-port 6080`
for a trusted VPN. For an existing TLS reverse proxy, add
`--workspace-url https://console.example`. Preserve its public Host and Origin
headers and forward WebSocket upgrades; the workspace checks that explicit
origin and issues Secure cookies. The proxy supplies TLS, not the VM service.
The wrapper selects the build UID/GID automatically. A copied `manage`
script and cached helper-image reference remain in the deployment directory for
routine operation without the checkout. Updating from source uses `deploy.sh`
from the newer checkout.

The wrapper needs Bash, Git for the initial build/update, and Docker with Compose.
It can use passwordless sudo for Docker when direct access is unavailable. Run
it as the ordinary data owner. A container-held file lock serializes deployment
mutations and releases when the wrapper's stdin closes, including after failure.

`up` checks available host memory and VM backing storage, builds missing images,
and waits for service readiness. It reuses existing VM images and disks.
Startup memory estimates and recording headroom are configurable under
`[capacity]`; measured available memory is not a reservation against other
workloads starting later.

To stop the whole installation, finish the meeting and verify retrieval, then
run `./deploy.sh down DIRECTORY`. The wrapper holds the host launch lock and checks both host bookkeeping and
guest recording ownership. Active or uncheckable guest capture blocks shutdown.
Use the workspace's **Stop recording** control for an individual meeting.

## Update and roll back the application

```bash
# From the newer committed checkout:
./deploy.sh update /path/to/deployment
# Restore the compatible previous application release:
./deploy.sh rollback /path/to/deployment
```

An update verifies its owned code artifacts, journals the pending release and
retains the previous build inputs/configuration and versioned local image tags.
It builds and replaces only `app`, `captions` and `workspace`, using `--no-deps`.
The VM and speech workers keep running. Caption transport retains its own retry
queue during the caption-service handover. The workspace login remains stable;
its viewer can reconnect afterward.

A failed build or activation restores the previous application files and tries
to reactivate that release. If interrupted, a pending receipt remains: use
`rollback` before another update or ordinary startup. Rollback refuses an
incompatible state schema. Guest disks, recording files and journals are retained.
Application rollback is not a backup or a guest-OS downgrade.

Configuration tuning is preserved. Edited generated code or changed service
identities require explicit maintenance instead of automatic replacement.
Keep the retained local images and `.releases` directory if rollback is needed.
The wrapper does not prune Docker images. Guest OS/browser maintenance is a
separate idle-VM operation; application update does not reboot or reprovision it.

## Build without installing Python on the host

From a clean, reviewed checkout:

```bash
git archive --format=tar HEAD | docker build \
  -f src/live_transcript/runtime/application.Dockerfile \
  --build-arg APP_UID="$(id -u)" --build-arg APP_GID="$(id -g)" \
  -t live-transcript-app:local -
```

The archive contains committed source only. The build validates the source,
builds the Python wheel, and packages the media tools. No virtual environment,
local runtime configuration or recordings enter this build context. Version pins
and checksums live inside the build; they are not requirements on host packages.
Keep the image tag/digest with the deployment configuration when upgrading.

## Provision the application and browser VM

Create a deployment directory outside the checkout and run these commands from it.
The helper keeps the same path inside the container so Compose bind mounts refer
to real host paths. Export as the ordinary user who will own the data.

```bash
docker run --rm --user "$(id -u):$(id -g)" --device /dev/kvm \
  -e LIVE_TRANSCRIPT_CONFIG= --workdir "$PWD" \
  --mount "type=bind,source=$PWD,target=$PWD" \
  live-transcript-app:local deployment "$PWD/stack" --managed-vm \
  --models "$PWD/models" --data "$PWD/data" --download-models

cd stack
docker compose -f compose.json build
docker compose -f compose.json up -d --wait --wait-timeout 2400
docker compose -f compose.json exec -T app live-transcript --json check
```

An installed CLI can export with the same `deployment` command. Without
`--download-models`, weights must already match their pinned checksums. The output
directory must be new and empty. All selected host ports must be unused.
`LIVE_TRANSCRIPT_CONFIG=` clears the application's runtime profile selection for
this one bootstrap helper; the generated services use their own mounted profile.

First boot downloads a verified Ubuntu image, installs Chromium and guest media
devices, reboots, and tests real audio capture and caption-camera output before
starting the app. Follow progress with `docker compose -f compose.json logs -f vm`.
Speech and caption services start independently during VM preparation. Image
builds and downloads can take several minutes; `up --wait` has a 40-minute budget.

The VM defaults to 4096 MiB RAM, 2 vCPUs and a 64 GiB virtual disk. Change those
at export with `--vm-memory-mib`, `--vm-vcpus` and `--vm-disk-gib`. The host-forwarded
SSH port defaults to 22222 (`--vm-ssh-port`). Caption and speech ports default to
8099, 9091 and 9093. They bind to host loopback. The guest reaches caption ingest
through a fixed-target relay on the private Compose network.

VM disks, serial/setup logs, identity keys and owner metadata live in `data-vm`,
next to `data`; `--vm-data` chooses another location. Keep it separate from app
storage and the deployment/build directory. The app receives only a read-only
operator key and pinned SSH trust. It cannot access the raw VM disk, administrative
key or QMP control socket. Only the VM sidecar receives `/dev/kvm`; neither it nor
the app runs privileged or mounts the Docker socket. Both run under the exporting
user's UID/GID. `--kvm-gid` can explicitly select the device's host group.

Retained VM disks and credentials are reused, never recreated on restart. A lock
prevents two runners from opening the same guest. Export refuses to adopt an
unowned directory or silently change retained VM resource/identity settings.
Back up both `data` and `data-vm`; deleting either bind mount is not an upgrade.
Guest originals remain inside the disk until a separate retention decision.

If first boot fails, inspect `data-vm/serial.log`, `qemu.log` and `bootstrap.log`.
An explicit retry is available only for a failed, never-initialized guest:

```bash
docker compose -f compose.json exec -T vm \
  python3 -m live_transcript.runtime.qemu --retry-setup
docker compose -f compose.json up -d --wait --wait-timeout 2400
```

Retry holds the guest meeting lease and refuses an initialized VM. It does not
replace the disk. A timeout while packages are still installing is not a reason
to reboot: inspect progress and repeat `up --wait` first.

## Use an already prepared VM

Without `--managed-vm`, export connects to a prepared VM and never starts or
provisions it. Select its private profile with pinned `vm.identity_file` and
`vm.known_hosts`; `vm.port` defaults to 22. Mount the profile and credentials in
the bootstrap directory and supply `--config "$PWD/profile.toml"` to the helper.
Omit `--device /dev/kvm` and add `--bind HOST_BRIDGE_IP` so that guest can reach
caption ingest. Use a trusted bridge address, not an unrestricted listener.

The same app/backend images run in both modes. Host networking lets the app reach
the existing VM bridge and its dynamically allocated console ports. Speech
workers and a managed VM use a private Compose network. The application uses a
local data volume shared with caption ingest for journals and final processing.
The optional notes endpoint must be reachable from both the app host network
and caption container; their loopback/DNS contexts differ.

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

The separate `workspace` service is available before and after meetings. Use
`./deploy.sh workspace DIRECTORY` for its stable login link and
[meeting controls and recording library](workspace.md). The `console` command
still opens a bounded viewer for an existing display. Open its private URL for
transcript and VM controls. With host networking, `6080` is a host port; adding a Compose `ports`
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
an unhealthy process. Restarting `app` leaves the VM running. A deliberate `stop vm`
or `down` requests guest ACPI shutdown and allows up to 120 seconds for guest
services to finish before terminating QEMU. Stop the meeting and verify retrieval
before intentionally stopping the whole stack. After a guest reboot, `start`
prepares its audio topology under the idle lease before launching the browser;
the persistent PulseAudio device script recreates its sink and microphone on
subsequent audio-server/guest restarts. Never restart or stop the VM as a response to a caption,
summary, application, or UI failure.

## Hermes

Configure Hermes to invoke a fixed wrapper around `docker compose -f ABSOLUTE_PATH
exec -T app live-transcript`, then use the same commands and notification contract
as the [native integration](hermes.md). Keep the Compose project and container
profile fixed in that wrapper. Do not have native cron scripts read the mounted
container state as if its PIDs belonged to the host namespace. The built-in
watchdog queues alerts but does not send messages or acknowledge delivery; Hermes
reads `notifications` and acknowledges only after successful authorized delivery.

## Optional assets

The standard image uses CPU speech and does not bundle optional Piper voices,
speaker-diarization models or GPU drivers. Supply those assets and runtime
configuration separately if enabling those features. The optional notes URL
must be reachable both from the app's host network and the caption container;
their loopback addresses and Compose DNS contexts differ.
