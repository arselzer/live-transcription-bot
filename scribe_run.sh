#!/usr/bin/env bash
# scribe_run.sh <meeting-url> [--max-min N] — Live Transcript host orchestrator: launches the VM if needed,
# joins the meeting from inside the VM, pulls the recording back, runs STT+summary, notes to vault.
# Robustness rules: joiner aborts must never strand a recording; every optional step is || true-guarded;
# a dead ssh tunnel cannot hang the script forever.
set -euo pipefail
umask 077
if [ "$#" -eq 0 ]; then echo "Usage: $0 <meeting-url> [joiner options]" >&2; exit 2; fi
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
CONFIG_SHELL=$(python3 "$SCRIPT_DIR/runtime_config.py" --shell)
eval "$CONFIG_SHELL"
export LT_ROOT LT_LOGS LT_VM_HOME
export LIVE_TRANSCRIPT_CONFIG="${LIVE_TRANSCRIPT_CONFIG:-$HOME/.config/live-transcript/config.toml}"
[ -n "$LT_VM" ] || { echo 'Configure vm.ssh before joining a meeting' >&2; exit 2; }
MEET="$1"; shift
python3 "$LT_ROOT/recording_storage.py" --prepare-link >/dev/null
mkdir -p "$LT_LOGS"
# Do not let concurrent launches wipe a live browser/caption session.
exec 9>"$LT_LOGS/run.lock"
flock -w "${LT_WAIT_FOR_LOCK:-0}" 9 || { echo "Live Transcript is already running" >&2; exit 3; }
case "$MEET" in *"'"*) echo "meeting URL may not contain a single quote"; exit 1;; esac
SSHK=(-o StrictHostKeyChecking=no -o ConnectTimeout=6 -o ServerAliveInterval=15 -o ServerAliveCountMax=8)
LT_RUN_ID="$(date -u +%Y%m%d%H%M%S)-$$"
export LT_RUN_ID

VM_IP="$LT_VM_IP"
if ! ssh "${SSHK[@]}" "$LT_VM" true 2>/dev/null; then
  [ "$LT_MANAGE_LIBVIRT" = 1 ] || { echo 'Configured VM unreachable' >&2; exit 1; }
  sudo virsh start "$LT_VM_NAME" >/dev/null 2>&1 || true
  for i in $(seq 1 40); do
    if [ -z "$VM_IP" ]; then
      VM_IP=$(sudo virsh domifaddr "$LT_VM_NAME" 2>/dev/null | awk '$3 == "ipv4" {print $4; exit}' | cut -d/ -f1)
      [ -z "$VM_IP" ] || LT_VM="${LT_VM%@*}@$VM_IP"
    fi
    ssh -o ConnectTimeout=3 "$LT_VM" true 2>/dev/null && break
    sleep 5
  done
  ssh -o ConnectTimeout=3 "$LT_VM" true 2>/dev/null || { echo 'VM unreachable' >&2; exit 1; }
fi
# A disconnected host must never reset a still-running detached meeting.
ssh "${SSHK[@]}" "$LT_VM" 'u=$(systemctl --user list-units --type=service --state=active,activating,deactivating --no-legend "live-transcript-meeting-*"); [ -z "$u" ] && ! pgrep -f "(^|/)join_m[e]eting.py( |$)" >/dev/null' \
  || { echo '[scribe] VM already has a meeting; reconnect or stop its session first' >&2; exit 3; }

# run bookkeeping for the watchdog/rejoiner
MJSON=$LT_LOGS/current_meeting.json
JLOG="$LT_LOGS/join-$LT_RUN_ID.log"
export LT_CONTROL_LOG="$JLOG"
mkdir -p "$LT_LOGS"
umask 077
chmod 600 "$MJSON" 2>/dev/null || true
ARGS_SAFE=$(python3 - "$@" <<'PY'
import pathlib, shlex, sys
args = sys.argv[1:]
secret = ''
for i, arg in enumerate(args):
    if arg == '--passcode' and i + 1 < len(args):
        secret, args[i + 1] = args[i + 1], 'REDACTED'
    elif arg.startswith('--passcode='):
        secret, args[i] = arg.split('=', 1)[1], '--passcode=REDACTED'
p = pathlib.Path(__import__('os').environ['LT_LOGS']) / '.passcode'
p.write_text(secret)
p.chmod(0o600)
print(shlex.join(args))
PY
)
python3 "$LT_ROOT/run_state.py" --begin "$MEET" "$@"
LT_DEADLINE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["deadline"])' "$MJSON")
export LT_DEADLINE
trap 'python3 "$LT_ROOT/run_state.py" "$MJSON" $$ --exit-code "$?" --control-log "$JLOG" 2>/dev/null || true' EXIT

if ! systemctl --user is-active --quiet captiond.service; then
  systemctl --user start captiond.service || echo 'WARNING: captions unavailable; source recording will continue' >&2
fi
# fail fast if the caption daemon isn't answering (live captions + chat commands depend on it)
printf 'GETGPU\n' | timeout 5 nc "$LT_CAPTION_HOST" "$LT_CAPTION_PORT" >/dev/null 2>&1 \
  || echo "[scribe] WARNING: captiond not answering on :8099 — live features may be dead" >&2

VM_CONFIG=$(mktemp "$LT_LOGS/vm-config.XXXXXX")
python3 "$LT_ROOT/runtime_config.py" --vm-config "$VM_CONFIG"
ssh "${SSHK[@]}" "$LT_VM" "mkdir -p '$LT_VM_HOME/.config/live-transcript'"
scp -q "$VM_CONFIG" "$LT_VM:$LT_VM_HOME/.config/live-transcript/config.toml"
rm -f "$VM_CONFIG"
GUEST_BUNDLE=$(mktemp "$LT_LOGS/guest-source.XXXXXX.tar.gz")
python3 "$LT_ROOT/deploy_guest.py" "$GUEST_BUNDLE"
scp -q "$GUEST_BUNDLE" "$LT_VM:$LT_VM_HOME/.live-transcript-source.tar.gz"
ssh "${SSHK[@]}" "$LT_VM" "tar -xzf '$LT_VM_HOME/.live-transcript-source.tar.gz' -C '$LT_VM_HOME' && rm -f '$LT_VM_HOME/.live-transcript-source.tar.gz'"
rm -f "$GUEST_BUNDLE"

# The VM worker prepares audio/browser state only after acquiring its lifetime
# lease; a second controller cannot race preparation against a new meeting.

MEET_SAFE=$(MEET="$MEET" python3 -c 'import os,urllib.parse; p=urllib.parse.urlsplit(os.environ["MEET"]); print(urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,"","")))')
echo "[scribe] joining $MEET_SAFE from VM $VM_IP"
# SSH executes a remote shell: preserve each argument, including display names.
REMOTE_CMD=$(python3 - "$MEET" "$@" <<'PY'
import os, shlex, sys
home = os.environ['LT_VM_HOME']
print("cd " + shlex.quote(home) + " && export LIVE_TRANSCRIPT_CONFIG=" + shlex.quote(home + '/.config/live-transcript/config.toml') + " LT_DEADLINE=" + shlex.quote(os.environ.get('LT_DEADLINE', '')) + " XDG_RUNTIME_DIR=/run/user/$(id -u) PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native LT_RUN_ID=" + shlex.quote(os.environ.get('LT_RUN_ID', 'test')) + " && exec " +
      shlex.join(['python3', '-u', home + '/meeting_session.py'] + sys.argv[1:]))
PY
)
systemd-run --user --quiet --collect --setenv="LIVE_TRANSCRIPT_CONFIG=$LIVE_TRANSCRIPT_CONFIG" --unit="live-transcript-mirror-$$" \
  --property=Restart=on-failure --property=RestartSec=5s \
  python3 "$LT_ROOT/archive_mirror.py" --vm "$LT_VM" --join-log "$JLOG" \
  || { echo '[scribe] could not start independent archive backup' >&2; exit 1; }
systemd-run --user --quiet --collect --setenv="LIVE_TRANSCRIPT_CONFIG=$LIVE_TRANSCRIPT_CONFIG" --unit="live-transcript-local-copy-$$" \
  --property=Restart=on-failure --property=RestartSec=5s \
  python3 "$LT_ROOT/archive_mirror.py" --vm "$LT_VM" --join-log "$JLOG" --lane local \
  || { echo '[scribe] could not start local safety backup' >&2; exit 1; }
touch "$JLOG"
while true; do
  set +e
  ssh "${SSHK[@]}" "$LT_VM" "$REMOTE_CMD" 2>&1 | tee -a "$JLOG"
  JOIN_STATUS=${PIPESTATUS[0]}
  set -e
  [ "$JOIN_STATUS" -eq 255 ] || [ "$JOIN_STATUS" -eq 75 ] || break
  echo '[scribe] SSH log connection lost; meeting service and archive continue. Reconnecting.' >&2
  if python3 - "$MJSON" <<'PYEND'
import json,sys,time
raise SystemExit(0 if time.time() > json.load(open(sys.argv[1]))['deadline'] + 900 else 1)
PYEND
  then echo '[scribe] control reconnect budget exhausted; VM recordings retained' >&2; exit 1; fi
  sleep 5
done
if [ "$JOIN_STATUS" -eq 0 ]; then python3 "$LT_ROOT/finish_meeting.py" "$MJSON" $$; fi
OUT=$(grep -oP 'DONE \K\S+' "$JLOG" | tail -1 || true)
[ -n "$OUT" ] || OUT=$(grep -oP 'RECORDING to \K\S+' "$JLOG" | tail -1 || true)  # salvage: joiner died mid-meeting
[ -n "$OUT" ] || { ARCHIVE=$(grep -oP 'ARCHIVE to \K\S+' "$JLOG" | tail -1 || true); [ -z "$ARCHIVE" ] || OUT="${ARCHIVE%.archive}.wav"; }
if [ -z "$OUT" ]; then
  if [ "$JOIN_STATUS" -eq 0 ] && [[ " $* " == *" --dry "* ]]; then exit 0; fi
  DIAG=$(grep -oP '^ADMISSION_DIAGNOSTICS to \K\S+' "$JLOG" | tail -1 || true)
  if [ -n "$DIAG" ]; then
    timeout 20 scp -qr "$LT_VM:$DIAG" "$LT_LOGS/admission-$LT_RUN_ID" \
      || echo '[scribe] admission screenshot retained in VM; retrieval unavailable' >&2
  fi
  echo "[scribe] no recording produced (see $JLOG)"; exit 2
fi
# Archival storage must never hold the meeting launch lock or block the next call.
flock -u 9
systemd-run --user --quiet --collect --setenv="LIVE_TRANSCRIPT_CONFIG=$LIVE_TRANSCRIPT_CONFIG" --unit="live-transcript-recovery-$LT_RUN_ID" \
  --property=Restart=on-failure --property=RestartSec=30s --property=Nice=10 \
  python3 "$LT_ROOT/recovery_worker.py" "$LT_VM" "$OUT" "$LT_RUN_ID"
echo "[scribe] local VM recording retained at $OUT; asynchronous archive and postprocessing queued"
[ "$JOIN_STATUS" -eq 0 ] || exit 1
