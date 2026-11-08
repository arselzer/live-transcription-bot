# Deployment architecture

The browser and original recorder run inside a VM. The application controls that
VM over pinned SSH. Live captions, recognition, notes and the operator workspace
are independent of the original capture process.

Two application deployments are supported:

- **Native:** `setup` provisions a managed VM and host tools/services. Host
  background jobs use systemd user services.
- **Docker:** the application image packages the CLI, UI/API, media tools,
  retrieval and final processing. Compose can provision a QEMU/KVM guest, or
  connect to a prepared VM. An application supervisor
  owns durable jobs; caption ingest and live speech have separate containers.
  See [application deployment](application-deployment.md).

A managed VM sidecar has a private Compose network, a persistent guest disk and
`/dev/kvm`. It does not require a host libvirt daemon or privileged container.
The app connects over pinned loopback-forwarded SSH and never receives the disk,
admin key or QMP socket. Guest caption traffic crosses a fixed-target relay.

## Ownership and failure boundaries

The guest session owns its meeting browser and lifetime lease. A separate guest
capture service owns lossless audio segments, screen video, snapshots and speaker
observations. Losing the host app, SSH log connection, speech server, summary
model or operator page must not terminate that recorder.

Host archive mirrors and local safety copies run independently. Final retrieval
and processing use durable checkpoints and retain originals when a step fails.
A successful process exit, caption connection or viewer connection is not proof
of a healthy recording; source progress is checked separately.

The Docker application persists its job queue and worker logs on local storage.
Worker identity includes the PID namespace and process start time. A scheduler
restart can adopt an existing wrapper; container replacement resumes bounded
retryable work. Meeting launches are not automatically replayed. The watchdog
checks the guest session before recovery, and calendar tasks revalidate their
source event. See [operations](operations.md) and [testing](testing.md).

## Storage and access

Keep SQLite/WAL and active VM disks on suitable local storage. Models are mounted
read-only, credential files stay outside image layers, and recordings/recovery
state stay on persistent volumes. Archive destinations may be separate disks or
mounted storage; their failure does not block local safety copies.

The operator API authenticates before exposing the selected VM's transcript,
console and bounded controls. It has no arbitrary command or proxy-target API.
The caption TCP protocol belongs on the trusted VM/application network.
Hermes invokes the same CLI and notification contract as a human operator; it is
not required for recording or the workspace. See [Hermes integration](hermes.md).
