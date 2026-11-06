# Live Transcription Bot

A self-hosted meeting recorder with live captions, meeting notes, a caption
camera, and optional text-to-speech. Operate it with **`live-transcript`**.
Zoom and Proton Meet adapters are included; Google Meet support is experimental.

## What you need

This is a Linux application with a browser VM, not a bot hosted by this repository.
Before joining a meeting, you need:

- A Linux host with Python 3.11+, FFmpeg, SSH, rsync, netcat, and systemd user services.
- A dedicated browser VM reachable over SSH. It needs Chromium/Playwright,
  Xvfb, PulseAudio, a dummy ALSA microphone, Pillow, and a V4L2 loopback camera.
- A working whisper.cpp build, a multilingual Whisper model and Silero VAD model.
- An optional OpenAI-compatible local model endpoint for meeting summaries.

If you already have that infrastructure, follow the steps below.
For preparing the VM and speech services, see [deployment](docs/deployment.md).
No model weights, recordings, VM image, or private configuration are included.

## 1. Install

From this checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[cpu]'
source .venv/bin/activate
live-transcript --help
```

The `cpu` extra provides the optional faster-whisper fallback for final
postprocessing. Live GPU/CPU speech services use your native whisper.cpp build.
The host does not need a browser; install Playwright/Chromium in the browser VM.

## 2. Configure your host and VM

Create a private profile. This example uses a separate filename so it does not
replace an existing installation's configuration:

```bash
mkdir -p "$HOME/.config/live-transcript"
export LIVE_TRANSCRIPT_CONFIG="$HOME/.config/live-transcript/bot.toml"
test -e "$LIVE_TRANSCRIPT_CONFIG" || install -m 600 config.example.toml "$LIVE_TRANSCRIPT_CONFIG"
```

Edit that file before continuing. At minimum, set:

| Setting | What to enter |
| --- | --- |
| `vm.ssh` | SSH destination for your prepared browser VM, such as `transcript@YOUR_VM_IP` |
| `vm.home` | The VM user's absolute home directory |
| `caption.host` / `caption.bind` | A host interface the VM can reach; use a trusted isolated network |
| `paths.whisper_cli` / `paths.whisper_server` | Your whisper.cpp executables |
| `paths.model` / `paths.vad` | Existing local Whisper and VAD model files |
| `paths.small_model` | Smaller local model for emergency CPU backlog fallback |
| `paths.cpu_model` | Local faster-whisper model directory for final CPU postprocessing |
| `paths.recordings` / `paths.safety_copies` | Private storage directories with adequate space |
| `model.url` / `model.name` | Your summary model endpoint and its served model name, if used |

`paths.root` is a **data workspace**, not the location of the source checkout.
`audio.sink` defaults to `transcript`; set it explicitly when reusing an existing
VM audio topology. Example IPs in the configuration must be replaced.

Verify SSH access and its host key using your actual destination:

```bash
ssh transcript@YOUR_VM_IP true
```

Keep the same `LIVE_TRANSCRIPT_CONFIG` setting in each operator terminal and in
service environments. You can also pass `--config FILE` before a CLI subcommand.

## 3. Start the local services

For an initial foreground setup, use separate terminals with the same private
profile selected:

```bash
live-transcript serve asr --device gpu
live-transcript serve asr --device cpu-large
live-transcript serve asr --device cpu-small
live-transcript serve captions
```

Each command stays running. The CPU standbys are used when the GPU fails or the
full model cannot keep up. For unattended use, install the service templates in
[deploy/systemd](deploy/systemd) and configure their executable/profile paths as
explained in [deployment](docs/deployment.md).

Then check readiness:

```bash
live-transcript doctor
```

Resolve failed checks before a real meeting. Doctor does not join a room.

## 4. Join and record a meeting

Use the full invitation and keep it quoted:

```bash
live-transcript start 'https://zoom.us/j/MEETING_ID?pwd=PASSWORD' --max-min 90
# Or:
live-transcript start 'https://meet.proton.me/join/id-ROOM#pwd-SECRET' --max-min 90
# Or (experimental):
live-transcript start 'https://meet.google.com/abc-abcd-efg' --max-min 90
```

For a separately supplied Zoom passcode:

```bash
live-transcript start 'https://zoom.us/j/MEETING_ID' --passcode 'PASSCODE'
```

Google Meet uses a headed browser controlled through loopback CDP, with a fresh
private context by default. Guest access still depends on the host and Workspace
policy. See [Google setup](docs/deployment.md#google-meet-browser).

The command deploys the current guest bundle automatically, joins the meeting,
and follows its log. If the room has a waiting room, the host must admit the bot.
Recording starts after confirmed admission. The display name comes from
`identity.name`; override it with `--name 'Meeting Transcript'` if needed.

A Proton invitation's `#pwd-...` fragment is required. Keep real invitations and
passcodes out of shared terminal logs and shell history.

## 5. Check progress and stop

In another terminal with the same profile:

```bash
live-transcript status
live-transcript stop
live-transcript jobs
```

`stop` asks the owned meeting session to close cleanly. After the meeting ends,
retrieval, final transcription, summary and video generation continue in a
background job. `jobs` shows their recent status and checkpoint locations.
Use `live-transcript --json status` or `live-transcript --json jobs` for automation.

Losing the SSH/log connection does not itself stop the detached guest recorder.
Use `stop` when you intend to leave the meeting.

## 6. Find the recording and notes

Look in your configured `paths.recordings` directory. The example profile uses
`~/.local/share/live-transcript/recordings`. A session produces:

| File | Contents |
| --- | --- |
| `SESSION.archive/` | Original lossless audio segments, screen video, snapshots, speaker evidence and checksums |
| `SESSION.wav` | Reconstructed audio for final transcription |
| `SESSION.txt` | Final timestamped transcript |
| `SESSION.md` | English meeting notes and the full transcript |
| `SESSION.srt` | Subtitles |
| `SESSION.mkv`, `SESSION-burned.mp4` | Derived video, when screen capture is available |
| `SESSION.live.json` | Independent live-caption evidence and saved decoder state |

If `paths.vault` is configured, notes are also copied into its `meetings/` folder.
Processing is not finished merely because the bot has left; check `jobs`.

Verify an archive or process a retained WAV independently:

```bash
live-transcript verify /path/to/SESSION.archive
live-transcript process /path/to/SESSION.wav
```

`process` expects 16 kHz mono PCM16 WAV. It retains the input and previous outputs
on recognition failure. For a failed VM retrieval, use the job's original token
and guest WAV path:

```bash
live-transcript recover RUN_TOKEN /home/transcript/rec/SESSION.wav
```

## Optional speech and Hermes

To speak explicitly into the active meeting:

```bash
live-transcript say --lang en 'The recording is running.'
```

TTS requires `paths.tts` to point to your private Piper installation and voice
assets; see [deployment](docs/deployment.md#optional-tts). Speech is never enabled
merely by starting a transcription session.

Hermes uses these same CLI commands. Configure its private skill to call
`live-transcript start`, `status`, `stop` and `jobs`, and schedule
`live-transcript watchdog` / `live-transcript calendar` if desired. The core
application has no Hermes SDK dependency. Calendar integration requires a private
ICS URL file and the correct `calendar.timezone`.

## Troubleshooting

| Symptom | First check |
| --- | --- |
| Bot stays in lobby | Host admission and the invitation; prejoin is not proof of entry |
| No live captions | `doctor`, caption/ASR services and VM-to-host connectivity; check that original capture continues |
| Bot has left but notes are missing | `jobs` and its checkpoint; final processing is asynchronous |
| GPU fails | CPU standby health and queued audio; preserve originals for full-model reprocessing |
| Speaker labels look wrong | Treat UI-derived names as uncertain, especially at speaker changes or overlap |
| NAS is unavailable | Local safety-copy status and retained VM originals; do not delete evidence |

See [operations and recovery](docs/operations.md) for failure handling. The caption
TCP protocol assumes a trusted network. Overlapping voices, poor audio and
provider UI changes remain limitations; automated tests do not establish word
accuracy or future meeting admission.

## Development and source layout

```text
src/live_transcript/   application package and internal process dispatcher
  runtime/launch.sh    packaged host orchestration
  providers/           meeting-platform browser adapters
  capture/             independent recording
  transcription/       live/final ASR and caption journal
  storage/             retrieval, mirroring and recovery
  notes/               meeting summaries
  presentation/        caption camera and video outputs
  speech/              explicit TTS
  core/                configuration and shared file operations
  integrations/        calendar scheduling
tests/                offline regression tests
tools/                explicit development/test tools
deploy/systemd/       service templates
```

There are no root-level script aliases. The VM receives one self-contained
`live-transcript.pyz` program, with external browser/media dependencies supplied
by the prepared VM. Normal installed wheels can build this bundle too.

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m tools.run_tests
.venv/bin/ruff check src tools tests build_backend.py
.venv/bin/ruff format --check src tools tests build_backend.py
.venv/bin/python -m tools.check_public_source --private-policy /path/to/private-policy.json
```

Source selection is explicit in `PUBLIC_SOURCE_FILES.txt`. Private policy,
recordings, assets, virtual environments and editable-install metadata stay out
of source history. Release builds enforce the source/privacy gate. Review the
Git author name/email and history separately before publishing.
A project license has not yet been selected.
