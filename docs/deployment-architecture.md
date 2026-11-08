# Deployment architecture review

The browser remains isolated in a VM. The supported default is a managed VM plus
native host speech services. Docker Compose is an optional host speech supervisor,
not a replacement for browser isolation. Hermes is a client of the public CLI.

The [portable backend export](container-backend.md) also packages the caption
daemon and durable journal with CPU speech. It removes host apt/systemd coupling
for those services; the VM/controller and final-processing boundaries remain
explicit. The journal is a persistent local bind mount with one owner, and model
failure cannot prevent caption ingest from starting.

## Approaches considered

| Approach | Benefits | Costs and failure boundaries | Decision |
| --- | --- | --- | --- |
| Managed VM + systemd user services | Independent guest kernel/devices; existing capture recovery; native GPU tooling; few runtime layers | Linux/KVM, initial sudo, guest OS upkeep and host units | Default; automate its installation and validate real media |
| VM + Compose speech | Packages native speech dependencies; explicit limits, health checks and model mounts; unchanged browser isolation | Docker daemon, another image/build lifecycle, GPU-specific images/toolkit | Optional CPU implementation; keep the same HTTP speech boundary |
| Browser in a container | Lightweight deployment on Linux | Current camera/microphone require host kernel devices; current guest supervision requires systemd; device access or privileged operation erodes isolation | Excluded by design and user requirement |
| Everything inside one VM | Packages more host dependencies | Couples decoding load and disk pressure to browser/capture; GPU passthrough adds host complexity | Do not make it the default |
| Rewrite all workers around an application supervisor | Could support more operating systems and generic containers | Must reproduce durable ownership, descendant cleanup, restart budgets and independent capture; large regression surface | Defer until a concrete deployment needs it |
| Kubernetes / distributed queue | Useful for many simultaneous rooms across machines | Scheduling, storage, secrets and recovery become distributed operations | Not justified for the current single-room installation |

Containers share the host kernel and require explicit device/capability choices;
see [Docker's runtime controls](https://docs.docker.com/engine/containers/run/).
Rootless Docker reduces daemon privilege but does not create a guest kernel:
[Docker rootless design](https://docs.docker.com/engine/security/rootless/).
These constraints, together with the existing media pipeline, are the basis for
retaining the browser VM rather than assuming that container packaging is enough.

## Supervision and ownership

Use one owner for each resource. The guest session owns its browser and lifetime
lease. An independent guest capture service owns lossless audio/video segments;
the session controller, live speech request, notes model and SSH log connection
must not be required for it to keep recording. Host mirrors and final processing
remain separate recoverable jobs.

Systemd is a suitable Linux process supervisor here: it tracks descendants,
records logs, supports bounded shutdown and can restart an owned service after
its parent disappears. It should not be treated as an implicit global API.
Service names are now configured, generated units are installation-scoped, and
empty unit names disable local start attempts for externally managed services.
Endpoint health and recording progress remain application-level observations.

Compose is useful for the HTTP speech workers. Its startup ordering alone does
not prove readiness: [Compose readiness documentation](https://docs.docker.com/compose/how-tos/startup-order/).
The optional generated project uses health checks and waits for healthy startup;
the installer also checks published host endpoints. A later unhealthy state does
not by itself trigger Docker restart policies. Router backoff and retained audio
are still necessary; restarting all services whenever recognition slows is unsafe.

## Changes from this review

- An installation command now provisions its VM, browser, virtual audio/camera,
  speech binaries/models, profile and service units. Its plan mode makes no changes.
- Workspace ownership, domain metadata and disk ownership guard against adopting
  existing resources. Interrupted setup retains its disks and resumes the same
  identity and ports. Edited configurations are never silently replaced.
- Browser Python dependencies use a guest virtual environment. Installing into
  distribution Python exposed both file-permission and package-manager conflicts
  in clean-VM testing; the managed path avoids that coupling.
- Separate operator and browser SSH keys restrict guest administration to setup.
  Host trust is provisioned from a generated guest host key, not accepted blindly.
- Setup checks actual audio energy and reads the virtual camera before reporting
  success. A reachable SSH daemon is not evidence of a usable recording pipeline.
- CPU-only installation uses the full primary model once, with a smaller standby;
  it does not load another identical full CPU model unnecessarily. CPU primary
  uses suitable timeouts and reports itself as CPU. Reduced-model use remains
  observable and full original audio remains available for final processing.
- Storage defaults to local directories. Archive destinations can be another
  local disk, mounted remote storage or another configured path. Nothing requires
  a NAS; mount-specific checks apply only when a required mount is configured.

## Remaining limits and next architectural work

Automatic provisioning currently targets apt-based x86-64 Linux/KVM hosts and an
Ubuntu guest. GPU builds rely on working vendor drivers/toolchains. Initial setup
needs network access and sudo; it is not an offline appliance or a shipped VM image.
OS repositories update independently of the pinned browser/model/source versions.
The optional speech-container path currently supports CPU, not arbitrary GPU images.

The host workflow still contains a shell orchestrator, and the guest capture path
intentionally uses systemd. Keep those adapters explicit; do not advertise generic
container or cross-platform operation until the same ownership, reboot, disk-full,
SSH-loss and crash drills pass there. A small supervisor interface can be extracted
when another backend is actually implemented; a speculative plugin hierarchy would
make the current deployment harder to maintain.

The caption/control protocol is unauthenticated and belongs on the trusted VM/host
network. Remote or multi-tenant deployment needs an authenticated transport before
exposure. Provider DOM changes and admission policies remain external failure
boundaries; infrastructure setup cannot guarantee that Google/Zoom will admit a bot.

Do not equate a green unit test suite, a healthy container or a complete setup
receipt with a successful real meeting. Keep installation, synthetic media,
provider admission, speech accuracy and archive retrieval as separate validation
results, and test the boundaries again after runtime changes.
