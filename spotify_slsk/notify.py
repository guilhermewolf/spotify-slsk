"""ntfy push notifications (optional)."""
import logging

import requests


def send_ntfy_notification(url: str | None, topic: str | None, message: str) -> None:
    # ntfy is optional. Without this guard an unconfigured deployment POSTs to
    # the literal URL "None/None" on every playlist of every cycle, which
    # raises, gets swallowed, and logs an ERROR — forever.
    if not url or not topic:
        logging.debug(f"ntfy not configured; skipping notification: {message}")
        return

    try:
        response = requests.post(f"{url}/{topic}", data=message)
        if response.status_code == 200:
            logging.info(f"Notification sent successfully: {message}")
        else:
            logging.error(f"Failed to send notification: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Error while sending notification: {e}")
