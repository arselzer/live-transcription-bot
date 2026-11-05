# Configuration

The tool reads `~/.config/live-transcript/config.toml`, using `XDG_CONFIG_HOME`
when set. `LIVE_TRANSCRIPT_CONFIG` or CLI `--config FILE` selects another file.
Known TOML values override defaults; unknown sections, misspelled keys and wrong
types fail early. Use `live-transcript --json config` to inspect effective values.
Do not put meeting passwords in this file. Runtime session arguments are private.

[config.example.toml](../config.example.toml) lists supported deployment settings.
Defaults live in one place: `runtime_config.DEFAULTS`. Setting `paths.root` also
derives state, logs, recordings, safety copies and model paths unless explicitly set.
Explicit paths expand `~` and environment variables. Use absolute guest paths.

| Section | Purpose |
| --- | --- |
| `paths` | Source checkout, runtime directories, local model/VAD/binary, optional vault, archive destination/mount |
| `vm` | SSH destination, libvirt name, IP, guest home, whether automatic libvirt start is allowed |
| `caption` | Trusted interface/port, speech endpoint, cadence, silence threshold and finalization delay |
| `asr` | CPU fallback/adaptation, endpoint timeouts, backlog/throughput thresholds, GPU/CPU device settings |
| `model` | Local OpenAI-compatible chat endpoint and served model name |
| `postprocess` | Full-model chunk length, independent repair length and per-decode timeout |
| `recovery` | Enable policy/model planning, observation interval, cooldown and action budget |
| `storage` | Local safety quota and host free-space reserve |
| `identity` | Transparent default participant name; command-line `--name` overrides it |
| `admission` | Test join limits; optional bounded Zoom name fallbacks and retry delay |
| `calendar` | Optional private ICS URL file |

The launcher exports only guest-relevant settings to the guest's default config
location. Host paths, vault and calendar settings are excluded. Both host and
guest need `runtime_config.py`; the launcher deploys it with the adapters.

Existing `CAPTION_HOST`, `CAPTION_PORT`, `CAPTION_BIND`, `CAPTION_STT_URL`,
`CAPTION_DB`, `CAPTION_CAP_FILE`, `SCRIBE_DIR` and `COMMIT_ONCE` environment
settings remain compatibility overrides where implemented. Prefer TOML for new
deployments. Systemd units must select the same configuration as the CLI.

An archive destination different from the recordings path uses a local symlink.
The launcher validates that symlink without touching NAS storage. Background
workers check the configured archive mount. An unset archive mount means ordinary
local storage; it does not promise an independent physical backup. Quota exhaustion
is reported and never triggers automatic deletion of unacknowledged evidence.

Protocol sample rate/PCM width, archival format and camera geometry remain
implementation contracts rather than arbitrary tuning knobs. Some legacy helper
scripts and benchmark entry points still contain installation-specific defaults;
use the standalone CLI and maintained launch/recovery paths for supported operation.

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

`paths.glossary` is optional and disabled by default. Set it explicitly to use a
private name/terminology list; no implicit file from the home directory is read.
`calendar.timezone` defaults to UTC for naive calendar entries and must name a
valid IANA zone. `postprocess.diarization_enabled` defaults to false and uses the
local `paths.diarization_model` when enabled. Voice clustering is experimental.
