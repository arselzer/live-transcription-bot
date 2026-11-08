# VM operator console

The browser stays in the VM. A temporary x11vnc server and websockify listen on
guest loopback; pinned SSH forwards them to host loopback. A Python HTTP backend
serves packaged Vue assets and proxies only that fixed noVNC upstream. It exposes
no general shell, file browser, arbitrary proxy target, or arbitrary keyboard
command. `live-transcript console --help` lists the bind address, advertised
hostname, lifetime, display, and control options.

## Everyday use

1. Run `live-transcript start 'INVITATION'` in your usual supervisor.
2. Once its VM display exists, run `live-transcript --json console` to observe it.
3. Open the `console_ready.url` from the output. Its full fragment is required.
4. To intervene, close that view-only console command and reopen with `--control`.
   This grants keyboard/mouse and a short renewable manual-control lease. Google
   and Proton prejoin actions, common UI recovery, Zoom camera recovery, and TTS
   dispatch yield to the lease. Capture continues independently.
5. Click a field inside the VM. Paste into the quick-input box. Automatic paste
   is enabled by default; turn it off to edit the text before sending.

The paste event works on HTTP over a VPN without browser clipboard-read
permission. The backend accepts at most 16 KiB UTF-8, passes text over SSH stdin,
and uses the X clipboard plus Ctrl+V. It never synthesizes Enter. Pasted newlines
still behave according to the focused application, just like ordinary paste.
The activity log records byte count, never text. The VM clipboard owner exits
after paste; the application you pasted into naturally retains its input.

Reconnect restarts only the viewer connection. Fullscreen enlarges only the
viewer. Escape, Tab, and address-bar shortcuts are the only exposed keyboard
API actions. Unknown actions, extra targets, and arbitrary shell commands are
rejected. Clipboard features require `--control` and a fresh lease.

## Status and failure behavior

Capture health comes from archive supervisor heartbeats for the selected display,
not from a connected VNC socket. A green viewer does not imply recording. Missing
or stale capture is shown explicitly. Audio/video/snapshot writers and free disk
space are shown separately. Speech queue and signal flags appear only while their
metrics are fresh; silence is distinct from recognition failure. Screen snapshots
can preserve speaker evidence when a manual browser has no DOM observer; this
does not claim reliable structured speaker attribution.

The guest display is pinned by Xvfb PID, process start time, user, display, and
private Xauthority file. A restarted/reused display is not silently controlled.
Lost SSH revokes the manual lease in at most 12 seconds after the last refresh;
the server stops its viewer helpers after 15 seconds without heartbeat. Existing
meeting capture lives in separate services. Diagnostic desktops have their own
bounded service and VM lease, so losing the viewer does not own their lifetime.
They close when their original `--max-min` budget ends; reopening a viewer does
not extend that budget. Diagnostic browsing is not an automatic recording mode.

The server monitors status even with no browser page open, emitting transitions
rather than repeating each poll. VM unreachability produces an alert and no
automatic restart or rejoin. The existing meeting watchdog retains its bounded
recovery policy. UI failure never kills a healthy recording as a repair action.

## Access and deployment

The capability URL uses a strong random token and a password in the fragment;
fragments do not appear in ordinary HTTP request URLs. Authentication exchanges
the token for an HttpOnly, SameSite=Strict session cookie. API, noVNC assets, and
WebSocket connections require it. Host and Origin checks protect mutations and
WebSocket upgrades. Runtime files and event feeds are private to the host user.
Each console uses a different cookie name: browser cookies do not isolate TCP
ports, so multiple VM consoles on one VPN hostname must not share a login cookie.

Default binding is `127.0.0.1`. `--listen 0.0.0.0 --advertise-host console.example`
is an explicit trusted-VPN choice. The built-in listener is HTTP. Do not expose
it directly to the public internet. A TLS proxy needs a matching explicit origin
configuration before use; the current built-in origin contract is HTTP to the
advertised host/port. Do not disable origin checks to work around proxy headers.

New managed guests install x11vnc, noVNC, websockify, xclip, and xdotool during
bootstrap. Existing guests need those packages installed before console use.
Assets ship in the wheel; no npm, CDN, remote font, or frontend build is required
at runtime. To develop the interface, edit `frontend/`, then run:

```bash
python -m tools.build_operator_ui
```

The helper uses pinned npm dependencies in a temporary build directory, leaving
node_modules outside the source tree. Commit the source, lockfile, generated
assets, and retained Vue license together. Run the documented source/privacy and
package checks after rebuilding.
