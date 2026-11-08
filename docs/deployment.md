# Deployment

Start with the [README walkthrough](../README.md) for installation, configuration,
joining and stopping. This page covers the infrastructure it requires. The
application runs on a Linux host and a prepared Linux browser VM. Hermes is
optional. `live-transcript setup` provisions this infrastructure automatically;
the manual instructions below also support existing or remote VMs.


## Automatic installation

Run `live-transcript setup --plan`, then `sudo -v` and `live-transcript setup` as
your normal user. Choose a new profile with global `--config FILE` when preserving
an existing deployment. Setup owns a dedicated libvirt domain, a new private
workspace, SSH keys and namespaced service units. It never adopts another VM or
replaces an unrelated configuration. It needs an apt-based x86-64 Linux/KVM host;
Ubuntu is the validated managed guest distribution.

Defaults are a 4 GiB / 2 vCPU browser VM with a sparse 64 GiB disk. `--memory-mib`,
`--vcpus`, `--disk-gib`, `--vm-storage`, `--vm-name`, `--network` and `--workspace`
select deployment resources. Keep VM disks and active journals on local storage.
The standard libvirt NAT/DHCP network is reused without restarting it; the guest
gets a MAC reservation so its private SSH address remains stable.

The Ubuntu image, whisper.cpp revision, model revisions and model/image SHA-256
checksums are pinned in the installer. Python browser dependencies use a dedicated
guest virtual environment. OS packages are updated from the distribution's
repositories; this is not a bit-for-bit frozen operating-system image.

Private setup state and `installation.log` are in the selected workspace. Rerun
the same command to resume. Verified downloads are reused. Failed guest dependency
setup can be retried with the operator-only administrative key, while holding the
VM's idle lease; a live meeting prevents repair. No guest disk or evidence is
deleted on failure. Setup never silently overwrites user-edited managed profiles.

`--recording-only` skips speech models and services. This is useful for validating
recording infrastructure; it deliberately provides no live transcription. The
ordinary doctor also checks speech dependencies. Use `doctor --recording-only`
to check capture without requiring speech weights or binaries. Speech endpoints
remain advisory in that mode. `--no-start-services` prepares full installation without
launching speech engines. Setup validates audio with a generated tone and reads
frames from the actual caption-camera device, without joining any provider.

The default speech backend is CPU, with large-v3 for accuracy and a small model
for backlog/failure recovery. `--backend cuda` or `--backend hip` selects a GPU
build using an already installed compatible toolchain. It does not replace GPU
drivers. Optional faster-whisper final fallback weights and Piper voices are
separate optional assets; the full native CPU path works without them.

For CPU containers choose `--speech-supervisor compose`. Setup generates a
private `compose/compose.json`, builds the pinned speech image, and starts only
its own project. The primary and small CPU server use read-only model mounts,
loopback-only published ports, non-root processes, no added capabilities, resource
limits and health checks. The browser remains in the VM. The setup state records
the exact Compose command for inspecting/stopping the project. Compose does not
restart a merely unhealthy process automatically; the speech router still handles
failed endpoints and retains queued audio. Read [deployment architecture](deployment-architecture.md).

## Host and models

Install Python 3.11+, FFmpeg, OpenSSH client, rsync, netcat, util-linux/flock and
systemd user services. The Python package installs Pillow. Libvirt tools and
permission to start the VM are needed only with `vm.manage_libvirt=true`.

Install the package in a virtual environment as shown in the README. An editable
checkout is useful for development; a regular wheel also contains the launcher
and can build the guest program. Neither installation includes models or native
media tools.

Build whisper.cpp for your hardware, including `whisper-server` and `whisper-cli`.
Set these **existing local files** in your private TOML profile:

| Key under `[paths]` | Required file |
| --- | --- |
| `whisper_server` | Native whisper.cpp server executable |
| `whisper_cli` | Native whisper.cpp CLI executable for final recognition |
| `model` | Full multilingual Whisper model, normally large-v3 |
| `vad` | Silero VAD model compatible with that whisper.cpp build |
| `small_model` | Smaller multilingual Whisper model for degraded live CPU operation |
| `cpu_model` | Local faster-whisper model directory for optional final CPU fallback |

Obtain weights from their upstream model registries and check their published
checksums. Put models and runtime journals on fast local storage. Live CPU standby
uses whisper.cpp; final CPU fallback uses the separate `.[cpu]` Python extra and
`paths.cpu_model`. Downloading a native `.bin` model does not prepare that fallback.

Set `model.url` and `model.name` for an OpenAI-compatible chat service if you want
meeting summaries. An unavailable chat model must not prevent source recording.
Set `recovery.model_enabled=false` to use only deterministic UI recovery.

## Persistent host services

The README shows how to run the four speech services in foreground terminals.
For unattended operation, first inspect the templates in `deploy/systemd/`.
Their `ExecStart` assumes an installation at `~/live-transcription-bot/.venv`;
change it to your actual installation. Use absolute executable paths in units.

A shell's exported profile is **not** automatically inherited by systemd services.
Each unit must select the same private profile as the operator. For example, add
this line under `[Service]` in each installed unit, adjusting the filename:

```ini
Environment=LIVE_TRANSCRIPT_CONFIG=%h/.config/live-transcript/bot.toml
```

Install the adjusted units without replacing a separate existing deployment's
units. On a new host:

```bash
mkdir -p "$HOME/.config/systemd/user"
cp deploy/systemd/*.service "$HOME/.config/systemd/user/"
# Edit the installed units' ExecStart and Environment before starting them.
systemctl --user daemon-reload
systemctl --user enable --now whisper-stt.service whisper-stt-cpu.service \
  whisper-stt-cpu-small.service captiond.service
systemctl --user status captiond.service whisper-stt.service
journalctl --user -u captiond.service -n 80 --no-pager
live-transcript doctor
```

CPU services disable GPU offload. The full CPU model is tried before the smaller
model; adaptive fallback trades accuracy for catching up. Both standby services
load their models persistently, so budget RAM and CPU accordingly.

Bind the caption TCP service to a trusted isolated host/VM network. Its control
protocol is unauthenticated. Choose an interface reachable from the VM; loopback
on the host is insufficient. Configure `caption.host`, `caption.bind` and port
consistently, along with any firewall rules. Use user-manager lingering if the
services must survive logout. A host reboot still interrupts capture.

## Browser VM

Provision a dedicated non-root user with SSH access, a persistent systemd user
manager, Python 3.11+, Playwright with Chromium, Xvfb/xauth, FFmpeg, Pillow,
PulseAudio/`pactl`, V4L2 loopback and a usable dummy ALSA microphone. Playwright
must be importable by the guest's `python3`; installing it only in the host's
virtual environment does not provide it to the guest.

Configure `/dev/video0` as a V4L2 loopback camera and grant the VM user access.
Use a managed PulseAudio user service/socket, disable ad-hoc autospawn, and load
the dummy ALSA module at boot. `audio.sink` names the dedicated output sink;
it defaults to `transcript` and is read from the same exported configuration by
capture and browser preparation. Existing VMs may need a different explicit name.
The browser's dummy microphone must be available before joining.

Headful Chromium runs under Xvfb and must receive decoded meeting audio.
Playwright's `--mute-audio` default is excluded for the recording participant.
Do not globally kill Xvfb, Chrome or PulseAudio to repair one session.

Set `vm.ssh` and the VM user's absolute `vm.home`, then verify SSH access and its
host key manually. The launcher uploads one `live-transcript.pyz` and a scoped
configuration to the guest. It starts a detached session under its lifetime
lease before preparing browser/audio state. The bundle contains application
code; external Python packages, browser binaries, media tools and kernel devices
must already exist in the VM. Source recordings stay in the guest's `rec/`
directory until an operator explicitly removes them.

Verify cold boot, SSH, media devices, host connectivity and an authorized test
room before relying on a newly provisioned VM. Offline tests do not verify this
infrastructure or guarantee admission to a provider.

## Google Meet browser

Google Meet is experimental. The default `browser.google_mode="cdp"` starts an
owned headed browser, connects over loopback, and uses a fresh private context.
It uses Playwright's installed Chromium binary; `browser.google_executable` can
select an absolute path to another installed Chrome binary in the VM. The
recorder keeps its explicit recording display name. No CAPTCHA is solved.

Joining uses verified native X11 input by default. Install `xdotool` and `xclip`
in the guest (on Debian/Ubuntu: `sudo apt-get install xdotool xclip`). Input is
restricted to the Meet page in the owned virtual display. Each target is checked
under the actual pointer before clicking; the entered name is verified exactly,
including Unicode. A failed verification stops the join and retains diagnostics.
`google.input_mode="dom"` selects ordinary Playwright input for diagnosis.
`google.admission_timeout` defaults to 330 seconds and never extends the original
meeting deadline. `doctor` reports missing native-input dependencies.

Google's independent audio, full-desktop video, snapshots, and browser speaker
observer start before browser navigation, so a failed join or manual takeover
retains evidence. Live captions and the convenience WAV start after positive
admission. Captions and speaker events use the archive's start clock; recovered
audio includes the prejoin interval. Failure screenshots and state transitions
are copied into the archive before finalization, including on failed admission.

After an automatic join error, `google.operator_recovery_seconds` keeps the
browser open for manual recovery (default 120 seconds; range 0–600). Open a
console with `live-transcript console --control` and complete the join there.
The console shows admission separately from capture health and counts down the
recovery window. The existing Hermes watchdog reports when operator action is
needed. The bot makes no join clicks, navigation, or identity changes in this
window. An explicit host denial ends the attempt immediately; the original
meeting deadline always caps recovery. Set the value to zero to finalize
immediately on error.

For meetings where unattended guest entry has not been validated, set
`google.manual_join=true` before starting the session. The browser opens the room
and waits up to `google.admission_timeout` for you to join through the console.
It then starts captions automatically. The requested display name is not
reported as a verified active identity after operator takeover. Browser state
and recordings stay inside the VM and the configured private recording storage.

Set `browser.google_private=false` to use the dedicated persistent
`chrome-meet-prof` profile, for example when an approved signed-in identity is
required. Use a dedicated, transparently named bot account. Provision sign-in
separately in that profile; the app does not accept passwords or solve sign-in
challenges. It checks for an existing Google session before navigating to the
meeting and stops if Google subsequently renders a signed-out name form. It never
silently falls back to anonymous joining. A session cookie is a readiness hint,
not a guarantee that the account still has access.

`google_mode="playwright"` retains the
ordinary Playwright launch path for diagnosis; Google can reject that launch
before the prejoin screen, even in an open room. Successful prejoin is not proof
of admission: recording requires both a leave control and an in-call control
such as Present now, with no waiting-room or pending participation notice.
Known Join now acknowledgements for existing recording/notetaker notices are
scoped to those dialogs. Starting a new recording/AI feature, switching an account
out of another call, and unfamiliar consent prompts require an operator.

Admission transitions, screenshots and the method/status of a refused Google RPC
are saved privately under `SESSION.admission/` and copied into the archive on
success. Cookies, request payloads and invitation queries are excluded from this
diagnostic record. HTTP 403 does not establish that the host denied the request
or that bot detection was its cause; these are distinct states.

[Google's access documentation](https://support.google.com/meet/answer/9302870?hl=en)
explains that Open, Trusted, Restricted and host-presence controls affect entry;
Workspace policy can impose further limits. A denial ends automatic admission
and saves private screenshot/reason evidence. Except for explicit host denial,
the configured operator window remains available. The application does not
rename itself or repeatedly retry a rejected Google admission.

A successful manual guest join does not establish that unattended joining will
work. Tests have observed automatic re-entry in an already-admitted browser,
followed by refusal after restarting it, even with the guest profile and browser
storage retained. Persistent guest state is therefore not a supported admission
fix. Keep the operator console available for recovery, and validate unattended
admission separately before relying on Google Meet for a scheduled recording.

## Storage

`paths.root` is the data workspace, independent of the source installation.
Configure recordings, local safety copies, and optionally an archive destination
and required archive mount. A slow or unavailable archive destination must not gate joining or
independent guest capture. Keep enough guest space for a whole meeting if copying
stalls. Quota/reserve failures are reported; they do not delete previous evidence.

A safety copy on the same physical disk as the VM protects against fewer failures
than a separate volume. Monitor free space on both. Final processing runs in the
background; use `live-transcript jobs` and the checkpoints/logs in `paths.logs`.
See [operations](operations.md) for verification and recovery.

## Optional TTS

Set `paths.tts` to a private directory containing this layout:

```text
tts/
  venv/bin/piper
  voices/en_US-lessac_medium.onnx
  voices/en_US-lessac_medium.onnx.json
  voices/de_DE-thorsten_medium.onnx
  voices/de_DE-thorsten_medium.onnx.json
```

Provide a working Piper installation at that executable path and the matching
English/German voice weights and configuration files. Assets are not bundled;
check their licenses before redistribution. These filenames are the current
supported voice mapping. The default directory is `paths.root/tts`.

With a meeting active, run `live-transcript say --lang en 'Test phrase.'`.
Speech is explicit, session-scoped and expiring. It is not enabled by starting
the recorder. Use an authorized test room to verify playback and mute-state
restoration before depending on TTS in a real meeting.

## Optional Hermes and calendar integration

Hermes should call the installed **public CLI**, with an absolute executable path
and the same `LIVE_TRANSCRIPT_CONFIG` as the operator. It needs no Python imports
or access to internal worker roles. Useful commands are:

```text
live-transcript start 'INVITATION' --max-min 90
live-transcript --json status
live-transcript --json jobs
live-transcript stop
live-transcript watchdog --dry-run
live-transcript calendar --dry-run
```

`watchdog` without `--dry-run` can repair/restart owned failed sessions within its
policy. Calendar integration reads `calendar.url_file`, a private file containing
a read-only ICS URL; set `calendar.timezone` for naive entries. Reconciliation
creates early-join user timers; dispatch revalidates an event before launch.
Run both dry runs before scheduling them. Neither dry run joins a meeting.

Preserve existing deduplication/watchdog state when migrating paths. Retain a
working installation until the replacement has passed an authorized live test.
Hermes reminder/alert delivery is outside the core recorder. No alert destination
is added automatically. Never change a scheduled real invitation while testing.

## Before a public release

Choose a license, review dependency/model licenses, audit Git history and author
metadata, and validate the documented setup on a clean host/VM. Run the private
source-policy check and build/test both wheel and source distribution. Runtime
configuration, meeting evidence, model assets and private policy belong outside
the source tree. Development test helpers are not a supported operator API.
