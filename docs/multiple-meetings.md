# Multiple meetings and audio bridges

Use one VM per active meeting, including a separate VM for a TTS test sender.
Each VM owns its playback, microphone, browser, recovery, and recording.

## Current CLI

Separate installations and profiles can run concurrently:

```bash
live-transcript --config /path/to/meeting-a.toml start 'INVITATION_A'
live-transcript --config /path/to/meeting-b.toml start 'INVITATION_B'
```

Each profile needs a different VM and host data root. Keep its caption daemon,
caption port, journal, logs, archive directories, and service names separate.
Changing only `vm.ssh` in a copied profile would leave ownership locks and state
shared. Provision each installation with `setup`, then select its profile for
every command.

ASR engine HTTP endpoints can be shared as externally supervised services;
leave their local service names empty in the secondary profile. Caption daemons
and journals stay separate. Engine queues, model memory, CPU threads, and GPU
capacity still limit the number of simultaneous meetings. Keep original capture
running when recognition falls behind, and report that delay explicitly.

Each profile has its own console. Choose a different host port for each. The
current UI controls one VM; it is not yet a fleet dashboard or a multi-user
tenancy system.

## Next architecture layer

A meeting manager should maintain an installation registry and allocate an idle
VM per session. A session ID should identify the meeting, VM, archive, caption
stream, and console capability together. Provisioning, meeting lifecycle,
capture, and inference remain separate components.

A fleet view can show admission, recording health, speech lag, storage, and
operator activity for each meeting, then open its existing console. Recovery
must target one session and VM. Losing the manager must not terminate capture.
Hermes should receive session-scoped links and incidents and use the same
structured controls as the operator UI.

A shared speech scheduler needs bounded queues and fair allocation per session.
Model fallback should be explicit and keep original audio for a later accuracy
pass. The current per-daemon ASR lock is not a fleet-wide scheduler.

## Bridging

Audio bridging is a separate, currently unimplemented feature. A route needs an
explicit source, destination, mode, gain, and expiry. Start with one-way playback
or TTS into a dedicated destination microphone. Bidirectional routes need
echo/loop detection, mix-minus audio, and an immediate mute control.

Archive each meeting's original incoming audio separately from bridge output.
Log route changes and playback timing so relayed speech is distinguishable from
the destination's own speakers. Summarized or translated TTS is derived output,
not an original recording. Meeting text and model responses must not create or
change routes; those are operator controls.
