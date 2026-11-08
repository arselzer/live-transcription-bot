# Meeting workspace

Start a recording, then open its workspace:

```bash
live-transcript start 'MEETING_INVITATION' --max-min 90
# In another terminal using the same profile:
live-transcript --json console --control
```

Open the full `console_ready.url` printed by the console command. Keep the console
command supervised while using the page. `--control` permits manual recovery;
omit it for a view-only console. For VPN access, add the listener and advertised
hostname shown in the [README](../README.md#view-and-control-the-vm).

## Read the meeting

**Transcript** is the default view. Each passage has a speaker label and time
relative to the recording. Recognition can revise recent text; UI-derived
speakers remain uncertain, especially during overlap. Speaker colors are stable
within the page and have no confidence meaning.

- Search by text or speaker. Search pauses follow-live while you read.
- Scroll up to pause following. **Follow live** clears search and returns to
  the newest passage.
- Use **Text** to change font size without changing the caption camera.
- **Save text** downloads the currently loaded passages as a timestamped text
  file. The page loads at most the latest 500 passages; search and download cover
  that window. Longer transcripts remain in the journal and final session files.
- **Meeting notes** appears when the configured model has produced a board.
  It is an AI draft, not a confirmed list of decisions or commitments.

The page reads committed text from the durable journal. Reloading it doesn't
reset recognition. A journal failure leaves the last readable transcript visible
with an error; it doesn't turn recording status green or stop capture. Switching
VMs clears the previous VM's text before loading the selected session.

The session is pinned to the archive's desktop process identity, rather than a
reused display number. Existing recordings started with older bundles can still
show capture health, but may lack the identity needed for this transcript view.
New sessions receive it automatically; don't restart an important recording just
to update its UI.

## Check recording and recover admission

The side panel reports audio, screen video and snapshot progress independently.
A connected viewer does not prove admission or recording. Stale progress is
marked unavailable; speech queue and detected signal describe recognition only.

When admission needs intervention, the page brings **VM console** forward once
for that incident and shows the remaining recovery window. It doesn't force you
back to that tab on every refresh. Use the VM browser to finish joining. Paste
into the side panel after selecting a field in the VM; no Enter key is sent.

Switching tabs keeps the viewer connection mounted. Reconnect affects the viewer,
not the meeting recorder. A closed page doesn't stop ordinary session capture.
The UI does not yet start/stop meetings or expose a recording library; use the
CLI to manage sessions and find the full [session outputs](../README.md#find-the-recording-and-notes).

## Use it through Hermes

After installing the [Hermes skill template](../examples/hermes/SKILL.md) and
configuring its private executable/profile, a request can be as simple as:

> Join this meeting for 90 minutes, record and transcribe it, and send me the
> workspace link when it's ready. Tell me if admission needs my help.

Hermes runs the same `start`, `console`, `status` and `watchdog` commands. It shares
the private workspace link in the authorized conversation and can forward
admission/recovery alerts. Sending an invitation alone doesn't grant the bot
meeting admission; the host may still need to admit it. The workspace remains
usable without Hermes. See [the integration contract](hermes.md) for supervisor
lifetime, early console creation and durable notification acknowledgement.

## About the demonstration

The README GIF uses the actual packaged Vue frontend and authenticated Python
API, backed by a temporary synthetic caption journal. Participant labels, text,
recording metrics and the illustrated VM view are fixtures. It is not evidence of
provider admission or recognition accuracy. No real meeting/browser profile,
clipboard contents, personal identity or invitation is used.

To reproduce it in a development checkout with Playwright and Chromium installed:

```bash
python -m tools.build_operator_ui
python -m tools.demo_operator_ui --output /tmp/live-transcript-demo
# If needed, add --chromium /path/to/chromium
```

The tool also checks search, text download, follow-live, tab persistence, recovery
handoff and layouts at narrow widths. Inspect the generated GIF before replacing
the reviewed documentation asset.
