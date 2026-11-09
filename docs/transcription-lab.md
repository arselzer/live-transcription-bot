# Jitsi transcription lab

The lab creates an unlisted UUID room on an explicitly allowlisted, self-owned
Jitsi server. It borrows a configured idle browser VM, starts two scripted
participants, and records the receiver with the normal independent archive.
It uses a separate caption process, journal, guest data directory and browser
profiles. It does not kill other browsers, change default audio devices, reset
another meeting, or reconfigure production services.

Install the application and prepare a dedicated test VM with the normal setup
or managed-VM deployment first. A reachable self-owned Jitsi instance and the
configured Piper voices are required for the default script. A custom `--plan`
uses supplied WAVs and needs no synthesis service. The lab creates the room by joining it;
it does not install a Jitsi server. Keep test credentials and endpoints in a
private TOML profile. Add the server hostname under `providers.jitsi_hosts`.
Caption transport uses an owned SSH reverse tunnel with loopback listeners, so
no additional host/guest ports need exposing. SSH port forwarding must be enabled
for the configured operator key. An occupied VM or desktop is refused.

## Run a test

```bash
live-transcript --config /path/to/test.toml --json lab jitsi \
  --server https://meet.example.org \
  --asr-mode gpu --scenario all --output /path/to/new-run
```

`--asr-mode gpu` starts and stops an owned full-model Whisper server using the
profile's binary/model paths. `cpu` starts the same full model on CPU. `existing`
(the default) checks and reuses the profile's inference endpoints; `--endpoint`
can select a different live endpoint without editing the profile. Existing
services are never stopped. An endpoint's health check proves availability,
not that its actual model/device matches a particular profile. Inspect the
retained engine configuration/logs before comparing performance.

`--scenario clean` is the default. `all` serializes clean, quiet, noisy and
overlapping scenarios. Quiet audio is attenuated 24 dB before transmission;
noise is seeded at 10 dB relative to the original clip's RMS (signal-to-added-noise ratio). Browser/network audio processing can alter
received levels, so these are sender conditions, not received-SNR measurements.
Each room has its own two participants and recording. Video transmission stays
muted. Original TTS files and the exact transformed sender files are retained.
The native and Docker application need their usual FFmpeg, SSH and rsync tools;
the guest needs Chromium/Playwright, Xvfb, PulseAudio and a systemd user manager.

`--silence-finalize 1.5` changes only that lab's caption silence boundary. Keep
other settings and inputs fixed when testing it. Smaller silence boundaries can
reduce latency but can also split hesitant speech, so compare accuracy first.
`--no-final` skips the independent full-model final transcription; it does not
turn a failed live recognizer into a passing one.

## Accuracy and regressions

Each run writes `report.json` and a Markdown table in `report.md`. Per-scenario
results include live/final transcripts, per-phrase coverage, possible missed
phrases, speaker observations, approximate first-caption latency, decoder/queue
measurements, playback timestamps and model/source fingerprints. Scoring reconstructs a timestamp-preserving WAV from the verified lossless
archive and retains the convenience WAV. Missing audio that intersects a
reference phrase is a capture failure, not an unexplained ASR regression.
The final pass
uses the normal full-model batch recognizer. Live `--asr-mode` selection does not
change that independent final recognizer's device/build policy. Input models are identified by file
metadata; that metadata is not a model-content checksum. The guest source bundle
and sender WAVs have content hashes. Available effective server settings are
saved by the sender; retain server image digests separately when operating it.

Non-overlapping speech uses Unicode case/punctuation-normalized word error rate,
with substitution/deletion/insertion counts. Overlap has no unique word order:
it uses unordered token recall/precision and does not receive an ordered WER.
Speaker windows and first-caption matching are heuristics; unknown and ambiguous
observations stay visible. Unmatched words require audio review before calling
them hallucinations. Synthetic scores do not establish human-meeting accuracy.

```bash
live-transcript --config /path/to/test.toml --json lab jitsi \
  --server https://meet.example.org --asr-mode gpu --scenario all \
  --baseline /path/to/previous-run/report.json --output /path/to/new-run
```

A compatible baseline must have the same reference, transformed audio, scenario
and phrase schedule. By default, an increase greater than two percentage points
in WER, a token-recall decrease greater than two points, or more possibly missed
phrases fails comparison. Faster captions never excuse worse accuracy. A short
script can cross that threshold with one word: inspect counts and repeat runs
before promoting a change. Without a baseline, completion means the test was
captured and scored; it is not an accuracy certification.

## Custom reference audio

Pass `--plan /path/to/plan.json` to replace the built-in bilingual script:

```json
[
  {"id":"one", "language":"en", "speaker":"Speaker A",
   "text":"A sentence with a checked reference.", "file":"one.wav"},
  {"id":"two", "language":"de", "speaker":"Speaker B",
   "text":"Ein Satz mit überprüfter Referenz.", "file":"two.wav"}
]
```

WAVs must be 16 kHz mono PCM16 and stay beneath the plan directory. They are
copied, never modified. Plans support at most three speakers, 100 phrases and a
15-minute scheduled budget and 1,500 normalized reference words. A single
speaker cannot schedule simultaneous phrases with itself. Optional `offset` values specify playback seconds
from the common start; otherwise scenarios generate their schedule. Reports
use the observed playback times. Very large transcript comparisons are refused
rather than allocating unbounded edit-distance matrices.

You can also score retained data without joining a room:

```bash
live-transcript --json lab report --reference reference.json \
  --transcript live-transcript.json --output rescored.json
```

Reference rows contain `id`, `text`, `language`, `speaker`, `start` and `end`;
transcript rows contain `t`, optional `end`/`speaker`, and `text`. A final-pass
JSON object with a `lines` array is also accepted. Add `--baseline`
with a previous score JSON to compare this offline score.

## Failures and recovery

The lab holds both the host launch lock and the VM's real meeting lease. Cleanup
addresses only its UUID service and its own process groups. It retains source
audio, archive segments, screenshots, speaker observations, caption snapshots,
logs, configuration and receipts even when a phase fails. Runtime limits bound
an unattended guest after SSH loss. No recording or VM disk is deleted.

Retrieve the exact run after a network or host interruption:

```bash
live-transcript --config /path/to/test.toml --json lab collect \
  /path/to/run/clean
```

Use the same VM/profile and its pinned SSH trust. Collection does not rejoin or
play audio; it verifies the archive before scoring and writes an updated scenario score.
The original top-level run report remains a historical record of that attempt. If its owned unit is still
running, wait for completion and collect again. A receipt for a different VM is
rejected. Failed or incomplete playback is not scored as successful recognition.
Treat lab output as private: it can contain room capabilities, installation
paths, settings, transcripts and recordings.
