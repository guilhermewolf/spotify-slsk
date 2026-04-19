FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HEARTBEAT_FILE=/tmp/heartbeat
ENV HEARTBEAT_STALE_SECONDS=900

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/

# Marks container unhealthy if the main loop hasn't touched the heartbeat
# file within HEARTBEAT_STALE_SECONDS (default 15 min). Matches a 5-min
# cycle with a 3-cycle grace window.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD [ -f "$HEARTBEAT_FILE" ] && \
        [ $(( $(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE") )) -lt "$HEARTBEAT_STALE_SECONDS" ] \
        || exit 1

CMD ["python", "app.py"]
