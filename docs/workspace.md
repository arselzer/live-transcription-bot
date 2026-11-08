# Meeting workspace and recording library

`live-transcript workspace` runs a persistent page for one VM. It stays available
between meetings and while the VM is booting or unreachable. Its private login
link remains valid across workspace restarts because the login key is retained
in local application state. Browser sessions still expire normally.

Docker deployments start this service automatically. Print its link with:

```bash
./deploy.sh workspace /path/to/deployment
```

For a native installation, run it independently of the meeting command:

```bash
live-transcript --json workspace --port 6080
# On a trusted VPN:
live-transcript --json workspace --listen 0.0.0.0 \
  --advertise-host console.example --port 6080
```

For TLS termination through an existing reverse proxy, set the external origin
explicitly with `--public-url https://console.example` and preserve Host/Origin
and WebSocket upgrades. This setting does not install a proxy or certificate.

Keep the process supervised. Native systemd users can run it as a user service;
Docker supervises it in the separate `workspace` container. The page is intended
for the installation's operator: its authenticated session grants access to
meeting controls and the retained recording library. `--view-only` disables
meeting mutations and manual VM control for that listener.

## Join, observe and stop

Paste a supported invitation, choose the maximum duration, and select **Join and
record**. The application queues the same supervised launch used by the CLI.
Admission, original recording and transcription have separate status indicators.
A queued launch or connected viewer is not confirmation that the bot entered.
The duration sets an absolute expiry at submission: a delayed controller cannot
start an old meeting request after that window. Retrying the same request keeps
its original deadline. The page reports an unavailable application controller
separately from VM capture.

**Stop recording** requires a second click and targets the meeting ID observed
by the page. A stale page cannot stop a newer meeting. Retrieval and final
processing continue in the background after the guest leaves.

The **Transcript** tab shows live captions, search, adjustable text size, text
export and optional draft notes. Follow-live can be paused while reading older
passages. Caption errors leave recording status and VM recovery available.

## Use the VM console

The viewer attaches automatically when the guest meeting browser becomes
available. An idle workspace does not create a desktop or reserve the VM, so it
can remain open before scheduled joins.

Select **Take control** before using keyboard/mouse or the paste panel. Control
pauses automated UI recovery through a short renewable lease. Release it when
finished; expiry also releases it when the page stops heartbeating. The lease is
scoped to the current desktop and cannot carry into a replacement meeting.
Changing control mode may briefly reconnect the viewer. Recording continues.

Click the destination field inside the VM, then use **Paste into VM**. It sends
text without pressing Enter. Keyboard shortcuts and reconnect affect the viewer;
they do not stop the meeting. The existing `console --idle --control` command
remains available for a bounded diagnostic desktop, which has its own VM lease
and does not record manually joined calls automatically.

**Listen** plays meeting audio from the guest's dedicated sink. Playback uses an
authenticated stream; it does not share your computer's microphone. Listening,
viewing and original recording have independent lifetimes.

## Review a recording

Open **Recordings** to browse or search retained sessions. Select a session to see
its saved transcript, notes, available downloads and capture/recovery events.
Click a transcript timestamp to play the corresponding point in the WAV. The
audio player supports seeking without downloading an entire long recording.
Playing a saved recording stops live listening to avoid mixing the two streams.

Final structured transcripts preserve speaker labels from the processing pass.
Older recordings can use subtitles, recognizer output or timestamped text; the
page identifies the source. Speaker observations remain uncertain, especially
near changes or overlapping voices. If a new structured transcript's audio
fingerprint differs from the retained WAV, timestamp playback is disabled until
the file is processed again.

Capture events use WAV-relative time only when the archive recorded its primary
capture start. Otherwise they show wall-clock time. Recovery event timing is
labelled estimated. The library never silently assumes that prejoin archive time
and final WAV time share the same origin.

The index runs in a bounded background process and stores private metadata/text
in `paths.state/library.sqlite3`. Recording storage outages leave the saved
catalogue available; missing media requests fail independently of live controls.
Indexing is incremental for large libraries. Refresh the saved catalogue manually:

```bash
live-transcript --json recordings --refresh
```

The library reads only configured recording and safety-copy locations. HTTP
requests select opaque catalogue IDs and named artifacts; file paths and symlink
escapes are rejected. Indexing and playback never modify original evidence.

## Capacity and background work

The capacity panel shows application storage, the filesystem backing a managed
VM disk, guest storage and estimated recording headroom. Its estimate includes
configured copy overhead and free-space reserves. It is a planning estimate,
not a guarantee about future video bitrate or memory availability.

New launches check capacity before beginning. A low-capacity incident during an
existing meeting raises an alert and retains existing evidence; it does not
stop that recorder or delete files.

Background work shows queued and running application jobs. **Cancel** applies
to standalone processing jobs. Retrieval and backup retain their own lifecycle.
Cancellation preserves the WAV and previously accepted outputs; a later
processing request can reuse its completed checkpoints.

## Hermes example

With Hermes pinned to the application's CLI wrapper:

1. Keep the workspace service running and share its stable private link in the
   authorized conversation before the meeting.
2. Queue `start 'INVITATION' --max-min 90`, or use the configured calendar.
3. Read `status`, capture progress and durable `notifications`; report admission
   and recording only after positive evidence.
4. Offer **Take control** if admission needs help. Read the same notification
   ledger after a workspace restart and acknowledge only delivered messages.
5. After stopping, check retrieval/processing jobs and the recording library.

The workspace never sends messages itself. See [Hermes integration](hermes.md)
for the delivery and acknowledgement contract.
