FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HEARTBEAT_FILE=/tmp/heartbeat
ENV HEARTBEAT_STALE_SECONDS=900

WORKDIR /app

# Dependencies first: this layer is cached unless requirements.txt changes,
# so an application-code edit rebuilds in seconds.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# Drop root in the image itself. docker-compose overrides this with
# user: "${PUID:-1000}:${PGID:-1000}" to match the host owner of the mounted
# volumes, but anyone running the published image directly (docker run,
# Kubernetes) previously got a root process — a needlessly wide blast radius
# for a daemon that parses audio files supplied by anonymous Soulseek peers.
USER 1000:1000

# Marks container unhealthy if the main loop hasn't touched the heartbeat
# file within HEARTBEAT_STALE_SECONDS (default 15 min). The daemon touches it
# per playlist and per track, so this distinguishes "wedged" from "slow", and
# keeps it fresh while waiting for an unreachable slskd so a dependency
# outage doesn't restart us.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD [ -f "$HEARTBEAT_FILE" ] && \
        [ $(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") )) -lt "$HEARTBEAT_STALE_SECONDS" ] \
        || exit 1

# Exec form (no shell) so PID 1 is python itself and receives SIGTERM
# directly — app.py installs the handler that drives the graceful shutdown.
CMD ["python", "app.py"]
