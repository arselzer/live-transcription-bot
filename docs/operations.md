# Operations and recovery

Use the same CLI/configuration for a human operator, cron, or Hermes. Inspect
`live-transcript --json status` before repairing anything. Doctor is read-only;
watchdog is repair-capable unless `--dry-run` is supplied.

## Failure decisions

| Symptom | Automatic behavior | Operator action |
| --- | --- | --- |
| SSH/control lost | Detached meeting and archive continue; reconnect follows the same token | Reconnect/status; do not start a duplicate |
| GPU ASR unavailable | Retain queued audio; route to configured persistent CPU engine | Check active engine, queue and service health |
| Full CPU model falls behind | With adaptive mode enabled, use smaller multilingual CPU model | Expect reduced accuracy; preserve originals for full-model reprocessing |
| GPU returns | Retry primary after circuit cooldown; restore full GPU model on success | Confirm backend/queue; no meeting restart needed |
| Both ASR paths fail | Captions wait; durable audio queue and independent archive continue | Repair engine; recover from originals if journal capacity is reached |
| Caption uploader/renderer dies | Adapter restarts owned convenience component | Verify archive before touching the browser |
| UI media control drifts | Semantic recovery candidates; bounded policy/local-model selection | Inspect `ui-health.json`, events and failure screenshots |
| Unsupported UI/HTML change | Escalate `needs_operator`; keep capture running where possible | Fix selectors or use a scoped operator session; do not blindly reload |
| Speaker selectors fail | Preserve empty/error observation and screenshot evidence; watchdog reports | Treat labels as uncertain; audio remains authoritative |
| NAS hangs | Independent local safety worker continues | Inspect local `backup-status.json`; primary VM originals retained |
| Safety quota/disk reserve reached | Report capacity limit, retain old and original files | Add space or choose retention explicitly |
| Capture writer crashes | Independent supervisor restarts with unique generation | Verify recovered gaps and alternate WAV coverage |
| Host denies/locks/CAPTCHA | Terminal state; no automatic repeated entry | Correct circumstances before explicit new join |
| Meeting ends/explicit stop | Graceful owner stop; finalize and retrieve in background | Verify finalized inventory, recovery status and expected duration |

Quiet/silent input and a disconnected media path are different. Neither a quiet
RMS reading nor absent captions alone authorizes restarting healthy recording.
A restart is reserved for confirmed dead ownership or sustained failure of both
independent raw audio writers. Recovery preserves the original deadline and has
bounded attempt/cooldown limits.

## UI/model rescue boundary

The local model receives only a bounded list of already-approved, visible media
or onboarding button labels. It returns a candidate ID, never code, selectors,
URLs or shell commands. The executor checks provider origin, original label,
visibility and allowed action again before clicking. Delayed plans expire.
Recovery cannot unmute, send chat, admit participants, leave/end a meeting,
navigate, reload or run arbitrary commands. Unknown controls require an operator.

A model outage falls back to the same local policy. At most the configured number
of applied recovery actions occur in five minutes. The Hermes watchdog exposes
unresolved UI recovery using existing alert delivery, rather than spawning an
unrestricted agent. This protects recording continuity and avoids meeting text
becoming instructions to an operator bot.

## Evidence inspection

A session archive includes `config.json`, `health.json`, `events.jsonl`, media
segments, snapshots, `speakers.jsonl`, `observer-health.json`, optional UI recovery
artifacts, chat/voice/notes evidence, `integrity.json` and `finalized.json`.
The final marker hashes the inventory; verification hashes actual file contents.
An existing directory is not proof of a completed or successfully backed-up call.

Use `live-transcript verify ARCHIVE` after finalization. `--evidence-only` skips
screen video when inspecting a critical safety copy. Check the reconstruction's
gap report and listen around uncertain boundaries. Never overwrite a primary
recording with regenerated audio. Retrieval retains the monitor WAV separately.

Recovery state files live under configured logs as `recovery-TOKEN.json`. A saved
recording checkpoint survives postprocessing failure, so retry does not download
it again. Postprocessing has bounded retries and leaves existing good transcript
outputs intact when recognition fails. `live-transcript recover TOKEN GUEST_WAV`
explicitly retries a failed job; do not associate scratch files from another call.

## Final transcript quality

A recognizer's zero exit code can accompany thousands of hallucinated repeated
phrases. Final recognition therefore uses bounded full-model chunks and checks
timestamp validity and sustained low-diversity phrase loops. Suspect chunks are
retried independently in shorter sections with decoder text context reset. A
surviving failure retains the rejected candidate, original audio and prior
outputs; it does not publish a new transcript or generate notes from that candidate.
These heuristics detect particular failures, not word accuracy. Natural short
acknowledgements are not automatically discarded.

Inspect `.transcript-quality.json`, `.transcription.json` and
`.postprocess-status.json` beside a recording. Processing status is also written
to local logs as `postprocess-SESSION.json` so watchdog inspection does not touch
the NAS. Recognition failures are reported by the existing watchdog. A per-recording
lock serializes reprocessing; a quality-checked cache keyed by audio SHA-256,
model file identity and decoder settings avoids repeating full recognition when
only summary/video generation is being retried. Changed audio invalidates it.

Retrieval exports the complete settled live caption journal to `.live.json`,
with a local copy under `state/live-evidence/`. It remains independent of final
recognition and includes review flags without deleting suspect text. A missing
or reset journal cannot erase an existing export. Final notes omit invitation
passwords from meeting metadata; private source metadata remains untouched.

The live export includes both the final checkpoint view and the card table,
plus the saved decoder state. Earlier checkpoint code could omit entries when
a full-utterance correction produced more than 12 segments. Updated checkpoints
persist the whole corrected view; incremental writes cover all newly appended
entries, even when a batch exceeds the camera's visible window.

## Before a real meeting

Run doctor, inspect current ownership and next calendar timer, confirm disk space
and backup status, then stop only known test sessions. Do not restart a healthy
speech engine or mutate PulseAudio/camera topology during a real call. A successful
prejoin or fixture test does not guarantee host admission. Keep meeting URLs,
passwords, calendar links and diagnostic screenshots private.

For a Zoom automation block, an explicitly configured `admission.zoom_name_fallbacks`
list permits at most two additional display names inside the same session lease
and original deadline. Host denial, locked meetings and CAPTCHA do not use this
sequence. Earlier failed attempts cannot mark a later successful recording as
terminal. See [Zoom admission](zoom-admission.md).

## Derived-artifact failures

Text, subtitle and video outputs use private temporary files and atomic
replacement. A failed encode leaves the previous file in place. A missing model, failed video,
or unavailable optional notes vault produces explicit `complete_with_warnings` status; an unexpected
stage error produces `processing_failed`. These statuses are visible to the
existing watchdog. Storage failures while publishing recognition caches do not
start a second CPU recognition pass. Original media is never removed.

The SQLite caption journal and atomically published derived files have
owner-only file permissions. Archive directories are private.
Reset/checkpoint bookkeeping stays within the same lock as the database commit,
so a reset cannot race the stored caption count for the next session.
