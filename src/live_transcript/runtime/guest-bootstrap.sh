#!/usr/bin/env bash
# Runs as root only inside a new managed browser VM, through cloud-init.
set -euo pipefail
# System packages must remain readable by the unprivileged browser account.
umask 022
install -d -m 0755 /var/lib/live-transcript
trap 'touch /var/lib/live-transcript/failed' ERR
export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=5 update
apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
  linux-generic linux-headers-generic \
  v4l2loopback-dkms python3-pip python3-venv python3-pil \
  ffmpeg xvfb xauth pulseaudio pulseaudio-utils alsa-utils \
  rsync xdotool xclip sudo dbus-user-session ca-certificates
python3 -m venv /opt/live-transcript/venv
/opt/live-transcript/venv/bin/python -m pip install 'playwright==1.64.1' 'Pillow==12.0.0'
/opt/live-transcript/venv/bin/python -m playwright install-deps chromium
runuser -u transcript -- env HOME=/home/transcript /opt/live-transcript/venv/bin/python -m playwright install chromium
cat > /etc/modules-load.d/live-transcript.conf <<'EOF'
snd-dummy
v4l2loopback
EOF
cat > /etc/modprobe.d/live-transcript.conf <<'EOF'
options snd-dummy index=0
options v4l2loopback devices=1 video_nr=0 card_label=LiveTranscript exclusive_caps=1
EOF
loginctl enable-linger transcript
install -d -m 0700 -o transcript -g transcript /home/transcript/rec /home/transcript/logs
printf '%s\n' "$1" > /var/lib/live-transcript/ready
rm -f /var/lib/live-transcript/failed
# cloud-init reboots into the generic kernel after this command succeeds.
