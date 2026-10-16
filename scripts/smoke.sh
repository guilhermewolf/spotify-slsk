#!/usr/bin/env bash
# Container smoke test: boot the built image the way an operator would, but
# with no secrets and no slskd, and check that it is usable and stops cleanly.
# The dashboard must come up without slskd/Spotify (it is how an operator
# diagnoses that outage), so every page below is expected to return 200.
#
#   scripts/smoke.sh [image]        (default: spotify-slsk:local; `make smoke`)
set -euo pipefail

IMAGE="${1:-spotify-slsk:local}"
PORT="${SMOKE_PORT:-18000}"
NAME="spotify-slsk-smoke-$$"
BASE="http://127.0.0.1:${PORT}"

fail() {
    echo "SMOKE FAIL: $*" >&2
    echo "---- container logs ----" >&2
    docker logs "$NAME" 2>&1 | tail -n 80 >&2 || true
    exit 1
}
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

echo "== static image checks"
docker run --rm --entrypoint sh "$IMAGE" -c '
    set -e
    [ "$(id -u)" = "1000" ] || { echo "image runs as $(id -u), expected 1000"; exit 1; }
    python -c "import numpy, spotify_slsk.daemon, spotify_slsk.webui"
    flac --version
    for p in /app/.env /app/data /app/playlists /app/slsk_app; do
        if [ -e "$p" ]; then echo "build context leaked $p into the image"; exit 1; fi
    done
'

echo "== boot without secrets or slskd"
# tmpfs volumes owned by uid 1000 stand in for the operator's bind mounts.
# Port 9 (discard) is never an slskd: the daemon must wait, not crash.
docker run -d --name "$NAME" \
    -p "127.0.0.1:${PORT}:8000" \
    --tmpfs /app/data:uid=1000,gid=1000 \
    --tmpfs /downloads:uid=1000,gid=1000 \
    --tmpfs /playlists:uid=1000,gid=1000 \
    -e SLSKD_HOST_URL=http://127.0.0.1:9 \
    "$IMAGE" >/dev/null

ok=""
for _ in $(seq 1 60); do
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != "true" ]; then
        fail "container exited during startup"
    fi
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")" = "200" ]; then
        ok=1
        break
    fi
    sleep 1
done
[ -n "$ok" ] || fail "/healthz did not return 200 within 60s"
echo "/healthz 200"

for path in / /settings /system/status /library-health /queue; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")"
    [ "$code" = "200" ] || fail "GET $path returned $code"
    echo "$path 200"
done

echo "== graceful shutdown"
start=$(date +%s)
# Grace period 20s: anything slower is SIGKILLed and exits 137, failing below.
docker stop -t 20 "$NAME" >/dev/null
elapsed=$(( $(date +%s) - start ))
code="$(docker inspect -f '{{.State.ExitCode}}' "$NAME")"
[ "$code" = "0" ] || fail "exit code $code after SIGTERM (expected 0)"
[ "$elapsed" -le 15 ] || fail "took ${elapsed}s to stop after SIGTERM (limit 15s)"
echo "stopped cleanly in ${elapsed}s"

echo "smoke test passed"
