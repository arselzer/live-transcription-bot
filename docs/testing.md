# Testing and evaluation

## Regression suite

Run `.venv/bin/python -m tools.run_tests` after installing the editable checkout.
The runner supplies isolated configuration and temporary runtime paths. Tests cover durable acknowledgments, replay, speaker timing, language
routing, caption correction, archive hashing/reconstruction, lifecycle leases,
launcher quoting, guest bundle execution, source privacy/index checks,
recovery checkpoints, configuration validation and recovery
policy boundaries. Unit tests do not prove a live provider join.

Postprocessing regressions include alternating hallucination loops, retained
rejected candidates, independent short-chunk retries, speech crossing chunk
boundaries, end timestamps, source-sensitive cache reuse and full live-journal
exports. Recognition quality gates are heuristics; use audio-backed human
references to measure accuracy rather than treating a passing gate as WER.

## Isolated integration drills

Run development tools from the checkout as `python -m tools.TOOL_NAME` (omit
`.py`). Tools that use a VM or live services require an explicitly selected
private test profile. Real-room helpers can join participants; GPU fault drills
can stop services, so do not run them during another meeting. They deploy a
separate `live-transcript-test.pyz`; the normal guest bundle excludes fixtures.

`smoke_caption_runtime.py` checks durable streaming/restart/lost-ACK behavior with
an isolated backend. Its synthetic latency is not real speech recognition latency.
`smoke_session_lifecycle.py` uses detached guest services to test SSH loss,
reconnection and ownership. `smoke_clean_stop.py` checks finite valid WAV headers
and archive finalization after a graceful owner stop.

`smoke_recording_guard.py --provider zoom|proton|google` runs inside the guest against
locally fulfilled pages and a private audio sink. It kills uploader/capture/guard
components while checking that evidence survives. It does not join a real room. Deliberately killed writers can leave partial
containers: the drill retains and reports them, and requires playable outputs
from surviving/restarted capture. Checksums alone do not prove decodability.

`smoke_asr_failover.py` performs actual inference while injecting primary backend
failure into an isolated router. It checks full CPU decoding, backlog-triggered
smaller-model decoding, and recovery to the GPU. It does not simulate a physical
GPU reset and must be described accordingly.

`smoke_ui_rescue.py` and `smoke_admission.py` exercise actual Chromium against
locally fulfilled provider pages. They check bounded model selection, stale plans,
provider-denial diagnostics and meeting-chat isolation without real joins.

## Audio accuracy matrix

`benchmark_audio_conditions.py --plan PLAN --output REPORT` transforms copies of
known WAV phrases: clean, -20 dB, -35 dB, clipping, 10 dB SNR noise, 0 dB SNR noise,
and timed dropouts. It reports normalized word errors, actual input metrics and
whether the live energy gate would skip the phrase. Direct ASR success without
checking that gate can hide lost quiet speech.

`benchmark_nonspeech.py` checks digital silence, noise levels, tones and hum with
the real engine in both languages. Zero hallucinations on synthetic nonspeech
is useful evidence, not proof against every real noise source. Acoustic quality
metrics are level/clipping observations; they do not claim SNR, speech detection
or overlap detection.

Test changing levels and language switches at utterance boundaries and midstream.
Use recorded/replayed audio with realistic reverberation and background speech
before deploying enhancement. Never replace raw input with denoised/normalized
samples: keep processing optional and compare word errors against the original.

`smoke_live_asr.py --mode quiet|fallback --plan PLAN --output DIRECTORY` runs an
isolated real-time STREAM2 wire replay. Fallback mode injects HTTP 503 errors
through a proxy, observes full CPU, smaller CPU and restored GPU decoding, and
retains submitted PCM, health, journal and transcript for inspection. It is not
a physical GPU reset. Audio-time silence boundaries must survive decode backlog.

## Upstream streaming comparison

`benchmark_whisper_streaming.py` imports an explicitly supplied, unmodified
`ufal/whisper_streaming/whisper_online.py` in a separate research environment and
adapts our existing whisper.cpp server to its OnlineASRProcessor. The report
records a source hash and caveats. Word-sized segments are approximate word
timestamps. Compute-only replay is not real-time meeting latency. This is a
LocalAgreement policy comparison, not an implementation of SimulStreaming's
AlignAtt backend. Keep upstream code out of the production import path.

## Real meeting tests

Use an explicitly authorized test room. `run_tts_e2e.py` and
`run_zoom_multibot_test.py` join temporary named TTS participants, record exact
playback times, compare known English/German phrases, test speaker switches and
include deliberate overlap. The latter excludes overlap from single-voice
baseline WER and reports it separately. Strict per-speaker WER can penalize a
correct transcript carrying an honest composite label; report text accuracy and
speaker attribution separately.

For overlapping voices, score each reference voice and inspect audio; a composite
speaker label does not prove both voices were transcribed. For actual failure
drills record the injected condition and its time, check the active ASR engine,
queue, raw capture, mirrored evidence and final checksums. Always end test bots
before a separately scheduled real meeting.

## Source and packaging privacy

Run the public-source checker with a private policy kept outside the checkout.
It checks the reviewed allowlist, unexpected source files, private keys/tokens,
non-example email addresses, local paths and configured private identifiers.
Git-index mode checks staged blob bytes. The ordinary scan also checks tracked
files inside ignored directories when a repository exists. Allowlists reject
traversal, source symlinks and oversized/binary source members. Checks are
fail-closed on unavailable or invalid policies; they are not an exhaustive
secret detector or a substitute for reviewing Git history and author metadata.

Builds enforce the same source gate. A source distribution includes only its
explicit manifest and must be tested by building a wheel from the extracted
archive, as well as directly from the checkout. Run the offline tests and
format/lint checks locally; no GitHub Actions workflow is bundled. These checks
do not join meetings or validate GPU performance.

Source-archive checks inspect tar ownership, group, PAX headers and timestamps,
not only file contents. The packaging hook normalizes these fields in a private
temporary archive before publishing the final artifact. Editable install metadata
contains local paths and is excluded from release/source selection.


Google native-input drills serve their page with Trusted Types enforced, matching
a real Meet constraint. Pointer calibration must use bounded direct reads, not
string-evaluating polling. The fixture checks Unicode case preservation, actual
pointer targeting, microphone mute restoration, speaker evidence and retained
media after capture failures. Passing it does not establish provider admission.

Wheel builds use a fresh snapshot of reviewed source. A previous setuptools
`build/lib` directory must not contribute deleted modules. Release verification
compares wheel member names and bytes with the reviewed package, in addition to
building from the source archive and scanning artifact contents for private data.

## Operator console

The review regressions exercise runtime provenance and legacy-directory import
shadowing, valid JSON watchdog output, read-only dry runs, durable notification
handoff before stdout, lost delivery, explicit/repeated acknowledgement, bounded
acknowledgement locks, and concurrent watchdog/receiver updates. A malformed
ledger must survive without resetting retry limits or dropping pending alerts.

Browser checks cover desktop and phone layouts, admission/evidence distinction,
the actual UTF-8 paste limit, retaining input on failed sends, copy-status fallback
over ordinary HTTP, and excluding tokens/passwords/input from copied diagnostics.
Use locators or direct reads to await UI state: the console's CSP rejects string
evaluation, and a live VNC WebSocket prevents generic network-idle waits.

`tests/test_operator_console.py` covers bearer/cookie authentication, Host/Origin
checks, unauthorized WebSockets, traversal, UTF-8 paste limits, key allowlisting,
private logs, PID reuse, expired control, partial SSH handshakes, bounded desktop
service ownership, pending/degraded capture transitions, and watchdog event
deduplication. No clipboard text or private error detail should reach events or
HTTP responses.

The live VM drill on November 7 connected the packaged Vue page through the
authenticated noVNC proxy to an existing Google call, confirmed fresh capture
health, and checked a narrow phone viewport for horizontal overflow. A separate
scratch Xvfb/Chromium desktop verified literal Unicode and multiline clipboard
input with no Enter events. The real meeting browser was not restarted for these
checks. Browser-only tests should use DOM readiness or explicit connection state;
an active noVNC WebSocket makes generic network-idle checks unsuitable.

The two-VM November 8 drill also opened both consoles in one browser context.
Both authenticated independently, kept their noVNC connections, and returned
status without repeated login requests. A shared-cookie-jar regression guards
against one console overwriting another's session on the same host/different port.

## Managed-install validation

The November 8 live drill exposed a whisper.cpp verbose-JSON failure after VAD
found no speech: optional language metadata raised a null-string exception and
the retained utterance blocked subsequent live chunks. A fresh isolated server
reproduced the HTTP 500. Retrying the same WAV with only language probabilities
disabled returned a valid empty result in 0.02 seconds. The client now performs
that bounded retry only for the specific exception, within the original timeout.
Other errors remain failures; audio, prompts, language selection, and recognition
thresholds are preserved. Regression cases cover the retry payload, time budget,
real text, empty results, normal language probabilities, and unrelated failures.

`install.sh --plan` and `live-transcript setup --plan` must work before a profile
exists, without changing the host. Offline regressions cover profile/domain/disk
ownership, modified profiles/units, invalid state, download checksums and symlinks,
private SSH transport, CPU-primary routing, and externally supervised services.

The November 6 deployment review exercised a newly created Ubuntu VM through
kernel/browser installation, reboot, real microphone/sink capture, virtual camera
frames and retrieval. It also resumed an interrupted bootstrap, repeated full
setup, built the pinned native speech binaries and verified all model hashes.
An isolated full CPU installation passed doctor and transcribed the pinned
whisper.cpp speech sample; this is not a provider admission or accuracy benchmark.
The sample took about 15 seconds for 11 seconds of input on that test host, so
CPU-only live throughput must be measured rather than assumed.

The optional speech image was built and its non-root/read-only runtime exercised.
Compose configuration, model mounts and endpoint readiness are separate checks
from browser isolation. No browser runs in a container. Existing installations
and live meeting services must not be stopped to run these drills; each test owns
its profile, VM, service names and ports.
