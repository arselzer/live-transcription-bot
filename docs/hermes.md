# Hermes integration

Hermes is an optional client of the standalone CLI. It does not own capture,
browser lifetime, meeting credentials, or the UI backend. Keep deployment paths,
VPN hostname, meeting invitations, and notification destinations in the private
installation/agent configuration, not in a committed skill or source file.

## Install and identify the runtime

Install and validate the standalone application first. Copy the reusable
[Hermes skill template](../examples/hermes/SKILL.md) into your private Hermes
skills directory and configure the executable and profile there. Do not copy
another installation's VM addresses, identities, paths, or delivery recipients.

Before scheduling an agent, run the exact executable that its scripts use:

```bash
live-transcript --config /path/to/profile.toml --json info
live-transcript --config /path/to/profile.toml --json check
live-transcript --config /path/to/profile.toml --json status
```

`info` reports the imported package, interpreter, selected profile, data root,
and VM name. This distinguishes an older executable on PATH from the maintained
package. Use the same explicit profile for every command, including console,
calendar, watchdog, recovery, and notification acknowledgement. Keep cron clients
pinned to that executable; do not use a second copy of the recorder as an adapter.

The readiness check reports `recording_ready` separately from `transcription_ready`. It checks
the speech health endpoint rather than treating a listening port as model
readiness. Normal check exits nonzero when live transcription is unavailable;
`check --recording-only` checks capture without requiring speech model files.
Neither command removes systemd resource holds or starts another speech model.

## Durable notifications and delivery

The watchdog stores notifications atomically with its event cursor and incident
deduplication state **before printing**. A failed stdout consumer or failed
messaging provider leaves them pending. Normal stdout remains a human-readable
compatibility interface; `--json` returns one versioned object:

```bash
live-transcript --json watchdog --dry-run
live-transcript --json watchdog
live-transcript --json notifications --limit 100
# Only after the external delivery has succeeded:
live-transcript --json notifications --ack NOTIFICATION_ID
```

The report separates current `issues`, newly emitted `messages`, stable-ID
`notifications`, and `pending_count`. A serialized concurrent watchdog reports
`busy` rather than starting another repair pass. `--dry-run` creates no watchdog
state or lock file and does not enqueue notifications or repair services.

Reading notifications does not acknowledge them. A receiver should deduplicate
by `id`, send only to the user's authorized conversation, and acknowledge only
the delivered IDs. Repeated recent acknowledgements are safe (the last 1,000
delivered receipts are retained); an unknown ID rejects the
whole acknowledgement batch. Unacknowledged repeats retain the same ID. The
acknowledgement lock waits at most five seconds; if busy, retry the same IDs later.
The
owner-only ledger is part of `watchdog-state.json`; malformed retry/delivery
state is retained and reported, never silently reset.

For a stdout-only consumer that cannot relay a delivery receipt, run
`live-transcript --json watchdog --ack-after-print`: the runtime prints the
report and then acknowledges that pass's own notification IDs. A failed print
leaves the ledger pending for the next pass. This hand-off is for a trusted
local agent runtime that has verbatim-rendered the messages; do not pipe into
an untrusted consumer first.

Each meeting run also produces one `recording-started` confirmation once the
provider reports positive admission and the archive exists, so a user knows the
bot joined without asking. Invitation passcode fragments (`#pwd-...`) are
stripped from notification text.

Hermes script-job stdout delivery alone supplies no delivery acknowledgement
back to the recorder. Such jobs remain best effort; their pending ledger can be
read by an agent or delivery adapter after an outage. Do not acknowledge a batch
merely because a cron script printed it. `created` and `last_seen` describe an
alert's age; an old pending incident is not proof of current failure. Verify a
console is still alive before sharing an old capability link, or open a fresh
console. The core application never calls a messaging provider.

## Console links and early joining

The same console URL opens the [meeting workspace](workspace.md): live transcript
first, with a VM console tab for admission and recovery. Share the full private
URL only in the authorized conversation. A new `needs_operator` incident opens
the console tab once; the agent should also deliver its existing recovery alert
for users who are not watching the page. The transcript UI is an observer, not
a substitute for watchdog delivery or durable recording.

Start the meeting using the normal `start` command, or let calendar dispatch
start it early. As soon as its browser display exists, supervise
`live-transcript --json console --listen 0.0.0.0 --advertise-host HOST --port PORT`
as an independent process. `console_ready` contains the complete private URL and
event-file path. Hermes can share that URL in the user's configured private
conversation before the scheduled meeting begins. Use view-only mode while
automatic admission is in progress; open `--control` when intervention is needed.

No automatic outbound message is sent by the console. The existing
`live-transcript watchdog` command reads recent active-console events and emits
ready links, warning transitions, and recovery messages for the existing Hermes
delivery adapter. Configure that adapter's destination privately and keep its
delivery authorization scoped to the user's conversation. Merely starting the
console does not install a cron job or change a production Hermes deployment.

An idle diagnostic console reserves the VM and is not a substitute for early
supervised joining. Do not run `console --idle` before a scheduled automatic join:
its VM lease deliberately prevents concurrent browser/audio initialization.

## Event contract

`live-transcript --json console` emits one JSON object per line. The same events are appended to
an owner-only `console-TOKEN.events.jsonl` under the configured host log path.
Each includes `id`, UTC seconds, `kind`, `level`, and a human-readable `message`.
`console_ready` additionally carries `url`, `event_file`, and owner identity.
The URL is a secret capability; do not post it publicly or quote it in an issue.

| Kind | Hermes behavior |
| --- | --- |
| `console_ready` | Share the private URL once, if delivery is configured. |
| `admission_needs_operator` | Offer the console within the bounded takeover window; preserve capture. |
| `admission_admitted` | Admission was confirmed; check capture progress separately. |
| `admission_rejected` | Admission was not confirmed; do not blindly relaunch. |
| `capture_attention` | Alert that healthy audio/video capture is unconfirmed. |
| `capture_pending` | Expected before admission; do not raise a recording incident. |
| `capture_healthy` | Report a recovery transition; do not infer intelligible speech. |
| `vm_unreachable` | Alert that VM status is unknown; capture may still be running. |
| `vm_reconnected` | Clear the connectivity incident. |
| `control_error` | Offer the console and private diagnostics; avoid blind retries. |
| `speech_degraded` / `speech_recovered` | Report recognition failure/recovery separately from capture. |
| `operator_interaction` | Observe that a human used the VM; no key values are recorded. |
| `operator_paste` | Observe paste byte count; text is never recorded. |
| `operator_key` | Observe an allowlisted shortcut. |
| `console_closed` | Retire the viewer link; this does not mean the meeting ended. |

Poll the file with an ID cursor; handle partial trailing JSON lines by waiting for
the next read. Deduplicate by ID. The watchdog retains a short cursor and verifies
the console owner's PID/start time before announcing its URL, suppressing stale
links. It never executes event contents as code. High-frequency interaction
events are rate-limited and kept out of routine watchdog notifications.

Keep recording, admission, ASR, and console states distinct. Report admission only
after positive meeting controls appear, and recording only after capture confirms
it. A user clicking Join, opening a viewer, or allowing media permission is not
proof of either. Do not reset audio, launch a second bot, or kill the browser to
repair the console. Manual control pauses the UI actions documented in
[console operations](operator-console.md); it does not suspend the recorder.
