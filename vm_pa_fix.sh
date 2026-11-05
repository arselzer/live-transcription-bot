#!/bin/bash
# vm_pa_fix.sh — ensure pulseaudio runs NON-IDLE-EXILING and scribe topology exists.
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native
python3 "$SCRIPT_DIR"/meeting_session.py --assert-idle || exit 3
if systemctl --user list-units --state=running --no-legend "live-transcript-archive-*" | grep -q .; then echo "Archive still running; refusing PulseAudio reset" >&2; exit 3; fi
PA=$(pgrep -x pulseaudio | head -1)
if [ -n "$PA" ]; then
    if ! tr '\0' ' ' < /proc/$PA/cmdline | grep -q "exit-idle-time=-1"; then
        echo "PA running with idle-exit — replacing"
        kill "$PA" 2>/dev/null
        sleep 2
        PA=""
    fi
fi
if [ -z "$PA" ]; then
    mkdir -p "$XDG_RUNTIME_DIR"
    setsid nohup pulseaudio --exit-idle-time=-1 --disallow-exit >/dev/null 2>&1 </dev/null &
    sleep 3
fi
python3 "$SCRIPT_DIR"/audioroute.py
