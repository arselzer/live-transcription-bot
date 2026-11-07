# Hermes integration

Hermes is an optional client of the standalone CLI. It does not own capture,
browser lifetime, meeting credentials, or the UI backend. Keep deployment paths,
VPN hostname, meeting invitations, and notification destinations in the private
installation/agent configuration, not in a committed skill or source file.

## Console links and early joining

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

`console --json` emits one JSON object per line. The same events are appended to
an owner-only `console-TOKEN.events.jsonl` under the configured host log path.
Each includes `id`, UTC seconds, `kind`, `level`, and a human-readable `message`.
`console_ready` additionally carries `url`, `event_file`, and owner identity.
The URL is a secret capability; do not post it publicly or quote it in an issue.

| Kind | Hermes behavior |
| --- | --- |
| `console_ready` | Share the private URL once, if delivery is configured. |
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
