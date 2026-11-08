# Configuration

The tool reads `~/.config/live-transcript/config.toml`, using `XDG_CONFIG_HOME`
when set. `LIVE_TRANSCRIPT_CONFIG` or CLI `--config FILE` selects another file.
Known TOML values override defaults; unknown sections, misspelled keys and wrong
types fail early. Use `live-transcript --json config` to inspect effective values.
Do not put meeting passwords in this file. Runtime session arguments are private.

[config.example.toml](../config.example.toml) lists supported deployment settings.
Defaults live in one place: `live_transcript.core.config.DEFAULTS`. Setting `paths.root` also
derives state, logs, recordings, safety copies, TTS assets and model paths unless explicitly set.
Explicit paths expand `~` and environment variables. Use absolute guest paths.

| Section | Purpose |
| --- | --- |
| `paths` | Data workspace, runtime directories, local model/VAD/binary, optional vault, archive destination/mount |
| `vm` | SSH destination, libvirt name, IP, guest home, whether automatic libvirt start is allowed |
| `console` | Isolated/shared login mode, private host credentials file, signed-session duration |
| `caption` | Trusted interface/port, speech endpoint, cadence, silence threshold and finalization delay |
| `asr` | CPU fallback/adaptation, endpoint timeouts, backlog/throughput thresholds, GPU/CPU device settings |
| `model` | Local OpenAI-compatible chat endpoint and served model name |
| `postprocess` | Full-model chunk length, independent repair length and per-decode timeout |
| `recovery` | Enable policy/model planning, observation interval, cooldown and action budget |
| `storage` | Local safety quota and host free-space reserve |
| `identity` | Transparent default participant name; command-line `--name` overrides it |
| `admission` | Test join limits; optional bounded Zoom name fallbacks and retry delay |
| `calendar` | Optional private ICS URL file and timezone |
| `audio` | Dedicated PulseAudio sink name; capture reads its monitor |
| `browser` | Google launch mode, private context, optional guest Chrome executable |
| `services` | Installation-scoped systemd units; empty names mean external supervision |
| `google` | Native/DOM join input and bounded admission timeout |

The launcher exports only guest-relevant settings to the guest's default config
location. Host paths, vault and calendar settings are excluded. The launcher deploys the
guest configuration separately from its application bundle.

Existing `CAPTION_HOST`, `CAPTION_PORT`, `CAPTION_BIND`, `CAPTION_STT_URL`,
`CAPTION_DB`, `CAPTION_CAP_FILE`, `LIVE_TRANSCRIPT_HOME` and `COMMIT_ONCE` environment
settings remain compatibility overrides where implemented. Prefer TOML for new
deployments. Systemd units must select the same configuration as the CLI.

An archive destination different from the recordings path uses a local symlink.
The launcher validates that symlink without touching archive storage. Background
workers check the configured archive mount. An unset archive mount means ordinary
local storage; it does not promise an independent physical backup. Quota exhaustion
is reported and never triggers automatic deletion of unacknowledged evidence.

Protocol sample rate/PCM width, archival format and camera geometry remain
implementation contracts rather than arbitrary tuning knobs. Development benchmarks remain explicit tools; use the public CLI for normal
operation. See [deployment](deployment.md) for external dependency and service setup.

`asr.gpu_device` is the HIP device index. `asr.metrics_device` is the separate
rocm-smi card key; the two numbering schemes can differ. Configure both explicitly
when multiple cards are present. CPU fallback uses no GPU offload.

`admission.zoom_name_fallbacks` defaults to `[]` and accepts at most two names.
It applies only to explicit Zoom automation denial. `name_retry_delay_seconds`
sets the pause; the original meeting deadline is not extended.

`postprocess.chunk_seconds` defaults to 300; `retry_chunk_seconds` defaults to 30
and must be between 10 and the chunk length. Chunks cannot exceed 1800 seconds.
`timeout_seconds` defaults to 1200 and bounds each recognition subprocess.
Shorter repair chunks reset decoder context, retaining the full original model.

`paths.state` stores local speech journals, service locks and postprocessing scratch.
The example places it outside the checkout. `paths.cpu_model` selects the
optional faster-whisper CPU postprocessing model directory.

`paths.postprocess_python` selects the interpreter for background postprocessing;
it must have the required ML dependencies. Source-checkout runs expose only the
checkout's `src` directory to that interpreter. For installed-wheel deployments, install the same bot
package version in the selected interpreter too; the application does not share
one interpreter's entire `site-packages` directory with another.
The foreground `live-transcript process` command uses the CLI's interpreter.

`paths.glossary` is optional and disabled by default. Set it explicitly to use a
private name/terminology list; no implicit file from the home directory is read.
`calendar.timezone` defaults to UTC for naive calendar entries and must name a
valid IANA zone. `postprocess.diarization_enabled` defaults to false and uses the
local `paths.diarization_model` when enabled. Voice clustering is experimental.


`vm.identity_file` and `vm.known_hosts` select installation-private SSH credentials
and pinned host trust consistently for control, recording retrieval and mirrors.
`vm.python` selects the prepared guest's Python executable. Managed setup uses a
dedicated virtual environment, leaving distribution Python packages untouched.
`asr.primary_device="cpu"` gives the full primary CPU-appropriate timeouts and
reports its engine correctly. With CPU primary, reuse that endpoint for
`asr.cpu_url` and leave `services.cpu_large` empty to avoid duplicate full models.
