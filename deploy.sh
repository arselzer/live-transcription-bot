#!/usr/bin/env bash
# Docker-only deployment helper; browser isolation remains a real KVM guest.
set -euo pipefail
usage() { printf '%s\n' 'Usage: ./deploy.sh {init|up|check|status|logs|workspace|update|rollback|down} DIRECTORY [init options]'; }
[ "$#" -ge 2 ] || { usage; exit 2; }
action=$1; target=$2; shift 2
case "$action" in init|up|check|status|logs|workspace|update|rollback|down) ;; *) usage; exit 2;; esac
[ "$(id -u)" != 0 ] || { printf '%s\n' 'Run as the ordinary user who will own the deployment.' >&2; exit 2; }
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
  "${DOCKER[@]}" info >/dev/null
fi
if [ "$action" = init ]; then mkdir -p -- "$target"; fi
root=$(cd -- "$target" && pwd -P)
script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
script_path="$script_root/${BASH_SOURCE[0]##*/}"
refresh_manager() {
  if [ "$script_path" != "$root/manage" ]; then cp --remove-destination -- "$script_path" "$root/manage"; fi
  chmod 700 "$root/manage"
}
image=${LIVE_TRANSCRIPT_DEPLOY_IMAGE:-}
if [ -z "$image" ] && [ "$action" != init ] && [ "$action" != update ] && [ -f "$root/.tool-image" ]; then
  image=$(<"$root/.tool-image")
fi
bootstrap() {
  if [ -z "$image" ]; then
    revision=$(git -C "$script_root" rev-parse --short=16 HEAD)
    image="live-transcript-tools:${revision}-u$(id -u)-g$(id -g)"
    if ! "${DOCKER[@]}" image inspect "$image" >/dev/null 2>&1; then
      git -C "$script_root" archive --format=tar HEAD | "${DOCKER[@]}" build \
        -f src/live_transcript/runtime/application.Dockerfile \
        --build-arg "APP_UID=$(id -u)" --build-arg "APP_GID=$(id -g)" -t "$image" -
    fi
  fi
}
helper() {
  bootstrap
  "${DOCKER[@]}" run --rm --user "$(id -u):$(id -g)" --network host \
    -e LIVE_TRANSCRIPT_CONFIG= --workdir "$root" \
    --mount "type=bind,source=$root,target=$root" "$image" --json stack "$root/stack" "$@"
}
compose() { "${DOCKER[@]}" compose -f "$root/stack/compose.json" "$@"; }
activate() { compose up -d --no-deps --wait --wait-timeout 180 app captions workspace; }
case "$action" in
  init|up|down|update|rollback)
    bootstrap
    lock_options=()
    if [ "$action" = down ]; then lock_options+=(--stop-guard); fi
    coproc OPERATION { "${DOCKER[@]}" run --rm -i --user "$(id -u):$(id -g)" \
      -e LIVE_TRANSCRIPT_CONFIG= --workdir "$root" --mount "type=bind,source=$root,target=$root" \
      "$image" --json stack "$root" hold-lock "${lock_options[@]}"; }
    operation_pid=$OPERATION_PID
    operation_input=${OPERATION[1]}
    read -r locked <&"${OPERATION[0]}" || { printf '%s\n' 'Another deployment operation holds the lock.' >&2; exit 1; }
    [ "$locked" = LOCKED ] || { printf '%s\n' 'Could not acquire deployment ownership.' >&2; exit 1; }
    trap 'exec {operation_input}>&-; wait "$operation_pid" || true' EXIT
    ;;
esac
case "$action" in
  init)
    for option in "$@"; do
      case "$option" in --data|--data=*|--models|--models=*|--vm-data|--vm-data=*)
        printf '%s\n' 'The wrapper keeps storage inside its deployment directory; use the export command for custom mounts.' >&2; exit 2;;
      esac
    done
    [ ! -e "$root/stack" ] || { printf '%s\n' 'Deployment already exists; use up or update.' >&2; exit 2; }
    bootstrap
    "${DOCKER[@]}" run --rm --user "$(id -u):$(id -g)" --device /dev/kvm --network host \
      -e LIVE_TRANSCRIPT_CONFIG= --workdir "$root" \
      --mount "type=bind,source=$root,target=$root" "$image" --json deployment "$root/stack" \
      --managed-vm --models "$root/models" --data "$root/data" --download-models "$@"
    printf '%s\n' "$image" > "$root/.tool-image"
    chmod 600 "$root/.tool-image"
    refresh_manager
    ;;
  up)
    running_text=$(compose ps --services --status running)
    running=()
    if [ -n "$running_text" ]; then mapfile -t running <<< "$running_text"; fi
    helper capacity --running "${running[@]}"
    for service in app captions primary vm; do
      service_image=$(compose config --images "$service")
      if ! "${DOCKER[@]}" image inspect "$service_image" >/dev/null 2>&1; then compose build "$service"; fi
    done
    compose up -d --wait --wait-timeout 2400
    helper url
    ;;
  check) compose exec -T app live-transcript --json check ;;
  status) helper status; compose ps ;;
  logs) compose logs -f --tail 100 ;;
  workspace) helper url ;;
  down)
    running_text=$(compose ps --services --status running)
    running=()
    if [ -n "$running_text" ]; then mapfile -t running <<< "$running_text"; fi
    helper guard-stop --running "${running[@]}"
    compose down
    ;;
  update)
    helper prepare-update
    if compose build app captions && activate; then
      helper commit-update
      printf '%s\n' "$image" > "$root/.tool-image"
      chmod 600 "$root/.tool-image"
      refresh_manager
    else
      helper abort-update
      activate || true
      printf '%s\n' 'Update failed; previous application release restored. The VM was left running.' >&2
      exit 1
    fi
    ;;
  rollback) helper rollback; activate ;;
esac
