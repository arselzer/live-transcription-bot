# Multiple meetings

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

Each profile has its own console server. Choose a different local port for each.
Set `console.login_mode = "shared"` and the same `console.shared_login_file` in
each profile to use one login across those consoles. Different files create
separate groups; isolated mode preserves separate logins. See
[console authentication](operator-console.md#access-and-deployment).

### One UI for several VMs

Keep the existing console servers running, then start a shared page. Run each
command in its own terminal:

```bash
live-transcript --config /path/to/meeting-a.toml console --idle --control --port 6081
live-transcript --config /path/to/meeting-b.toml console --idle --control --port 6082
live-transcript --config /path/to/meeting-a.toml console-hub \
  --console meeting-a=6081 --console meeting-b=6082 --port 6080
```

Open the private link printed by `console-hub` and select a VM in the header.
The transcript, browser view, paste actions, recording status and activity follow that VM.
Switching views clears pending paste text and does not stop either recording.
The hub proxies the viewer and WebSocket connections through one browser origin.
Console names are configurable lowercase identifiers; names and ports are not
accepted from browser requests.

For VPN access, give all three commands the same `--advertise-host VPN_ADDRESS`;
add `--listen 0.0.0.0` to the hub. The underlying console servers can remain bound
to loopback. All profiles and the hub must use the same shared login file.
Existing isolated consoles require reconfiguration before adding them to a hub.
An unavailable console does not prevent selecting another VM. Closing the hub or
a viewer leaves the independently supervised VM capture running.

The hub connects existing consoles; provisioning, VM allocation, bridging and
multi-user access control remain separate work.
