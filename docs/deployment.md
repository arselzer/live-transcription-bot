# Deployment

The supported installation currently consists of a Linux host source checkout
and a prepared Linux guest. Hermes is optional. Packaging installs the CLI;
use `pip install -e .` and retain the checkout for launch/service/adapter scripts.
A fully bundled wheel/container and automated guest provisioning are future work.

## Host

Install Python 3.11+, FFmpeg, OpenSSH client, rsync, netcat, util-linux/flock,
systemd user services and Pillow. Install libvirt tools only when
`vm.manage_libvirt=true`. Put models and runtime journals on fast local storage;
copy large weights off a slow NAS before enabling live services.

Build whisper.cpp for the target hardware. Configure the server/CLI paths and
multilingual large-v3 and Silero VAD paths. CPU standby additionally needs the
configured smaller multilingual model. Obtain models from their upstream model
registry and verify published checksums; no model weights are included here.

Create `~/.config/live-transcript/config.toml` from the example. Configure an
OpenAI-compatible chat endpoint for summaries and bounded recovery planning, or
disable model recovery and operate without summaries. Model unavailability must
not prevent source recording. Explicit speech requires local Piper and the
English/German voice files under `tts/` in the configured checkout. Existing TTS
asset names are currently an implementation convention.

Service templates are in `deploy/systemd/`. They use `~/live-transcription-bot` as an
example checkout and the default config location. Adjust paths or add service
overrides for another deployment. CPU services use `--no-gpu`, preserve the full
model initially, and keep smaller-model decoding confined to degraded operation.
The current host runs the speech services persistently so fallback does not need
a cold model load after failure.

```bash
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whisper-stt.service whisper-stt-cpu.service \
  whisper-stt-cpu-small.service captiond.service
```

Use an isolated trusted bridge for the caption TCP service. Do not expose its
unauthenticated control protocol to an untrusted network. Run the user manager
with lingering if services/timers must survive logout. A host reboot still
interrupts capture; local persistence is not high availability across machines.

## Guest

Provision a non-root user reachable by SSH, Python 3.11+, Playwright with Chromium,
Xvfb/xauth, FFmpeg, Pillow, PulseAudio and `pactl`, V4L2 loopback and a usable dummy
ALSA microphone. Configure the V4L2 device `/dev/video0` for the caption camera.
The current adapters use a sink named `scribe` and its `scribe.monitor` source;
those are protocol/device conventions still awaiting a broader device abstraction.

Use one managed PulseAudio user service/socket, disable ad-hoc autospawn, and load
the dummy ALSA module at boot. The virtual browser microphone preserves the real
device ID/label. Headful Chromium must run under Xvfb and receive decoded audio;
Playwright's default `--mute-audio` argument must be excluded for the recording
participant. Do not globally kill Xvfb or Chrome to repair a session.

The launcher validates idle ownership, deploys maintained modules plus a scoped
guest configuration, then starts a detached session. The source bundle includes the package layout
and compatibility bootstraps; configuration is transferred separately. Preparation requires the
VM lifetime lease before changing audio/browser state. Verify cold boot, media
devices and a real authorized test room before relying on a newly provisioned VM.
Do not treat this document as an automated provisioning script.

## Storage

Choose configured recordings, archive destination and safety-copy directories.
A separate mounted archive volume is checked only by background workers; joining
must remain independent of NAS availability. Keep enough local guest space for a
whole meeting if copying stalls. Local safety quota/reserve failures are reported;
they do not delete old evidence. A safety copy on the same physical disk as the
VM protects against a different set of failures than a separate NAS copy.

## Optional calendar and Hermes

Run the standalone CLI directly for manual operation. For cron, use
`live-transcript watchdog`; existing Hermes wrappers call the same maintained
`meeting_watchdog.main`. There is no Hermes import/dependency in the core CLI.
Watchdog state is stored under configured logs. Migration of an existing install
must preserve old watchdog/calendar dedupe state before changing paths.

Calendar integration reads a private read-only ICS URL file. The reconciler
creates exact early-join systemd timers; the dispatcher revalidates the event at
launch. Existing Hermes reminder delivery remains an integration concern, not a
requirement for recording. No new alert destination is configured automatically.
Use `calendar_meeting_launch.py --dry-run` to inspect the existing integration.

Guest and host service units must use the same effective configuration as their
operator. `LIVE_TRANSCRIPT_CONFIG` is forwarded to host background jobs. Do not
change a real meeting's invitation/timer while testing a personal room.

## Before a public release

Choose a license and review dependencies/model licenses, remove deployment
credentials/runtime evidence, provision a clean host/guest from documentation,
add reproducible service/container packaging, and test provider changes with
real authorized rooms. Current legacy helper scripts and historical benchmarks
are not a supported portable API. Preserve tested compatibility entry points until
all schedulers/operators have migrated to the standalone CLI.
