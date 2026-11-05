# Research and decisions

Reviewed 2026-11-05 using upstream repositories and project documentation. These
are design observations, not copied implementations or promises of equivalent
performance. Keep licensing and hardware/backend constraints visible before
reusing code.

| Project | Useful approach | Decision here |
| --- | --- | --- |
| [WhisperStreaming](https://github.com/ufal/whisper_streaming) | LocalAgreement confirms a prefix repeated across successive decodes; rolling context and timestamp trimming | Benchmark its unmodified online processor against our current pipeline using the same whisper.cpp server |
| [SimulStreaming](https://github.com/ufal/SimulStreaming) | Successor with AlignAtt and LocalAgreement streaming policies | Research candidate; not a drop-in replacement for the current AMD whisper.cpp deployment |
| [WhisperLive](https://github.com/collabora/WhisperLive) | Separate streaming clients/backends, optional VAD/diarization and explicit degraded behavior | Keep capture independent of inference and make backend health/queue visible |
| [pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | Regular diarization plus an exclusive single-speaker view for transcript alignment | Preserve overlap/ambiguity evidence; do not confuse a single-label display with successful recognition of two voices |
| [Vexa](https://github.com/Vexa-ai/vexa) | Self-hostable meeting API, distinct capture/transcription/control surfaces | Standalone CLI/configuration and optional agent integrations |
| [Attendee](https://github.com/attendee-labs/attendee) and [lifecycle docs](https://docs.attendee.dev/guides/basics) | Explicit joining, joined/not-recording, recording, ended and failure states | Separate meeting, raw archive, captions, UI, backup and recovery health; source-available licensing requires separate review |

## What has actually been tried

The initial bot already had a custom streaming Whisper pipeline: persistent GPU
inference, overlapping windows, revisable provisional text and final-utterance
correction. Its existence did not mean upstream WhisperStreaming had been tested.
The new comparison imports the unmodified upstream OnlineASRProcessor with a
custom whisper.cpp HTTP bridge and requests word-sized timestamped segments.
That preserves the current AMD engine while evaluating the upstream policy.

On six synthetic English/German phrases, the upstream policy comparison produced
one word deletion out of 46 reference words; the prior real-Zoom replay through
our current pipeline produced zero edits on those 46 words. The comparison is
small, word timestamps are approximate, and the replay paths differ. It does not
justify replacing the accuracy-first production pipeline. A larger paired corpus
with recorded conversational turns, interruptions and noise is required.

A full large-v3 CPU whisper.cpp test took roughly 8.7 seconds for 2.8 seconds of
English speech. A separate faster-whisper int8 CPU test took roughly 3.3 seconds
plus model loading; these are single-sample compute timings, not meeting latency.
An incompatible PyAV file-decode call was encountered in the latter stack;
passing PCM arrays avoided it. The production fallback uses the existing
whisper.cpp HTTP contract and does not add that Python dependency.

## Overlap and speaker identity

Overlapped speech detection identifies simultaneous activity; diarization assigns
speakers; separation attempts to recover each voice; ASR recognizes words. These
are different tasks. A mixed Zoom sink plus active-speaker DOM cannot guarantee
both overlapping transcripts. Composite labels preserve uncertainty instead of
forcing one speaker. Raw stereo capture and timed speaker/snapshot evidence remain
available for offline analysis.

Participant-separated tracks through a supported provider media API are a strong future
option because they avoid mixed-voice separation and much DOM speaker inference.
They require provider integration/credentials and deployment testing; they are
not silently enabled by installing a diarization package. pyannote community-1
also requires accepting its model access conditions before downloading weights.

## Audio levels and enhancement

The real-engine degradation matrix found recognizable -35 dB speech was skipped
by the original live RMS gate. Lowering the gate to 40 retains that tested speech;
separate synthetic silence/noise/tone/hum checks produced no decoded text. Keep the
high-confidence no-speech override's stronger energy check independent of this
lower floor. Heavy noise still produced German errors. No destructive denoising
or automatic normalization is applied to original recordings.

Adaptive CPU fallback is an availability measure when the preferred GPU backend
fails. Backlog or sustained real-time factor above the configured threshold can
select a smaller multilingual model. Active engine/degraded state and queue are
reported. Full-model GPU decoding resumes after successful recovery. Smaller-model
live output must remain distinguishable from a later full-model transcript.

## Verified failure-path results

The corrected real-time failure replay retained all six bilingual turns and
showed full CPU → smaller CPU → GPU recovery, with a peak queued-audio backlog
of 21.75 seconds that drained to zero. It had one word error out of 46; smaller
model fallback is explicitly degraded. The first replay exposed a lost turn when
processing-time pause detection merged queued languages. Pause boundaries now
use captured audio time, covered by a regression and a repeated wire replay.

An actual personal-room test after the configuration refactor produced zero
edits across the 46-word baseline with two named senders. A later stress retry
was incomplete because Zoom explicitly blocked one automated participant; it is
not counted as a successful two-sender or live GPU-failover test. The main archive
remained healthy and both finalized archive/safety-copy inventories verified.

Current Zoom bot-protection and SDK guidance are covered separately in
[Zoom admission](zoom-admission.md). RTMS is the documented AI-notetaker input
path; older Meeting SDK bot examples should not be treated as current policy.
