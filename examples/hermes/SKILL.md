---
name: live-transcription-bot
description: Operate the standalone meeting recorder, preserve evidence, and report admission, recording, and transcription separately.
---

# Meeting recorder operations

Configure the absolute `live-transcript` executable, private TOML profile, and
authorized notification destination in your local agent configuration. Use that
same executable and profile for every operation. This template contains no real
meeting links, identities, paths, or messaging credentials.

## Before starting

1. Run `live-transcript --config PROFILE --json info` and confirm the intended
   package, interpreter, profile, data root, and VM name.
2. Run `doctor` and `--json status` with that profile. For an intentionally
   recording-only installation use `doctor --recording-only`.
3. Preserve the complete invitation, including Zoom's password query or Proton's
   encryption fragment. Keep it out of public output and shell history.
4. Start using `live-transcript --config PROFILE start 'INVITATION' --max-min N`.
   Supervise the host controller independently. Never run a raw provider joiner
   alongside an owned meeting, and never run setup against a live installation.

## Communicate observed states

- A running controller is not proof of admission. Wait for positive in-call
  controls or an admission-success report.
- A connected viewer is not proof of recording. Confirm archival audio/video
  progress and its fresh heartbeat. Report degraded capture explicitly.
- ASR, caption camera, speaker labels, summaries, and backups have separate
  health. Their failure alone must not terminate healthy original capture.
- Google can archive join evidence before admission. Describe this as join
  evidence, not a captured meeting. Unsigned Google admission is experimental.
- A host denial, locked room, or challenge needs operator attention. Do not
  invent identities, change networks/profiles, solve challenges, or retry loops.
  Only the user's explicitly configured bounded Zoom name fallback is eligible.

## Console and recovery

Once a meeting display exists, run `live-transcript --json console`. Supervise it
separately; share its full private URL only in the authorized conversation.
Use view-only mode during automatic joining. Use `console --control` to
intervene; this pauses eligible automatic UI actions and leaves capture running.
Do not use `console --idle` to prepare a scheduled meeting: its diagnostic
desktop reserves the VM and does not automatically record.

Google's configurable operator recovery window keeps its browser open after
automatic join errors. An explicit host denial is terminal. `google.manual_join`
can leave joining entirely to the operator; captions start after confirmed entry.
Recovery never extends the original session deadline.

If SSH disconnects, reconnect to the exact owned session. Unknown status is not
proof of a dead bot. Use `status` and the console; never reset audio, kill all
Python/Chrome processes, remove display locks, or launch a duplicate participant.
Use `stop` for the selected session when the user intends to leave.

## Notifications

`--json watchdog --dry-run` inspects decisions without repairs or state writes.
The ordinary watchdog is repair-capable. `--json notifications` lists durable
pending alerts with stable IDs; reading does not acknowledge delivery.

Deliver only to the configured authorized conversation, deduplicate by ID, and
run `--json notifications --ack ID ...` only after delivery succeeds. For a
trusted stdout-only cron consumer, `watchdog --ack-after-print` acknowledges
that pass's own notifications after a successful print. Never
acknowledge merely because output was read or printed. Retry an acknowledgement
after a lost reply; the last 1,000 delivered receipts are idempotent. An old pending notification is historical
evidence, not necessarily a current incident. Check that a console capability is
still alive before forwarding it. The built-in stdout cron path is best effort
and provides no return delivery receipt.

## Completion and retained evidence

After leaving, inspect `jobs`; retrieval and final processing are asynchronous.
Verify `finalized.json` and checksums using `verify ARCHIVE`. Use the original
host run token and guest WAV path with `recover` for failed retrieval. A session
WAV stem and a host run token are different identifiers.

Retain original audio, video, snapshots, speaker observations, and unacknowledged
audio chunks. Derived transcripts or summaries can be wrong; report processing
failures and uncertain speaker labels. Do not claim intelligible captured speech
from a healthy process or synthetic fixture alone. Apply the user's separate
note-formatting, redaction, and sharing instructions to derived deliverables.
