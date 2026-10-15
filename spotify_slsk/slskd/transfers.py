"""slskd downloads: enqueue a candidate, follow its transfer, find the file."""
import logging
import os
import time

from .. import config
from ..db import add_blocklist, add_tried_file, record_history
from . import client as slskd_client
from .models import Transfer, TransferOutcome, remote_basename

# The candidate dict behind the most recent successful download_and_verify,
# so the caller can attribute a post-download rejection to the peer file
# (blocklist) and record where the file came from (history). Daemon-thread
# only.
last_download = None



# slskd can report "Completed, Succeeded" while the file still sits in its
# incomplete directory; the move to the final location lands moments later.
COMPLETION_GRACE_SECONDS = 30


def find_file_in_downloads(filename, base_dir=None):
    """Path of a *finished* download named `filename`, or None. slskd's
    incomplete directory is never searched: a hit there is still being
    written."""
    if base_dir is None:
        base_dir = config.DOWNLOAD_DIR
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d != "incomplete"]
        if filename in files:
            return os.path.join(root, filename)
    return None


def _await_completed_file(filename, grace=None):
    """Poll for a finished download for up to `grace` seconds (1 s steps,
    interruptible by shutdown). Returns the path or None."""
    deadline = time.time() + (COMPLETION_GRACE_SECONDS if grace is None else grace)
    while True:
        path = find_file_in_downloads(filename)
        if path or time.time() >= deadline:
            return path
        if slskd_client.interruptible_sleep(1):
            return None


def _candidate_history(candidate) -> dict:
    return {
        "username": candidate.get("user"),
        "filename": candidate.get("filename"),
        "format": (candidate.get("ext") or "").lstrip(".") or None,
        "bitrate": candidate.get("bitrate"),
        "size": candidate.get("size"),
    }


def grab_candidate(candidate, conn, playlist_name, track_id, source="auto"):
    """Enqueue one candidate, wait for it, and return the local path or None.

    Any failure is remembered in the track's `_tried` table and history.
    Only a definitive peer-file fault — slskd reporting the transfer
    failed/errored/rejected/aborted (`candidate["peer_fault"]`, set by
    wait_for_completion) — also blocklists the file for every track. Our
    own wait timeout, a "Queued, Remotely" stall, slskd not listing the
    transfer yet, a file we could not find afterwards, or a shutdown say
    nothing certain about the peer, and a global ban would outlive the
    track's retry. On success the candidate is stashed in `last_download`.
    `source` ("auto"/"manual") is recorded in history.
    """
    global last_download
    candidate.pop("failure", None)
    candidate.pop("peer_fault", None)
    basename = remote_basename(candidate["filename"])
    logging.info(
        f"Attempting download: {candidate['filename']} from {candidate['user']}"
    )
    try:
        slskd_client.get_client().transfers.enqueue(
            username=candidate["user"],
            files=[
                {
                    "filename": candidate["filename"],
                    "size": candidate["size"],
                }
            ],
        )

        file_path = wait_for_completion(candidate)
        if file_path:
            logging.info(f"Downloaded and verified: {file_path}")
            if _wait_for_external_processing(file_path):
                last_download = candidate
                record_history(
                    conn, "grabbed", table_name=playlist_name, track_id=track_id,
                    source=source, **_candidate_history(candidate),
                )
                return file_path
            logging.warning(f"Post-download verification failed for: {basename}")
            failure = "file did not appear after download"
        else:
            logging.warning(f"Download failed or was not confirmed: {basename}")
            failure = candidate.get("failure") or "transfer failed"
        add_tried_file(conn, playlist_name, track_id, basename)
        # A shutdown mid-transfer says nothing about the peer.
        if not slskd_client.shutting_down():
            record_history(
                conn, "download_failed", table_name=playlist_name,
                track_id=track_id, reason=failure, source=source,
                **_candidate_history(candidate),
            )
            if candidate.get("peer_fault"):
                add_blocklist(
                    conn,
                    candidate["user"],
                    candidate["filename"],
                    candidate.get("size"),
                    failure,
                    track_id,
                )
    except Exception as e:
        # Don't blacklist on this path: an exception here is usually a
        # transient slskd-API hiccup (JSON decode against an empty body,
        # connection reset, etc.), not a "this peer rejected the file"
        # signal. add_tried_file is permanent until the track succeeds,
        # so a single bad response would permanently ban a perfectly
        # good candidate. Let the next cycle retry naturally — bounded
        # by MAX_ATTEMPTS_BEFORE_SUSPEND on the track itself.
        logging.error(
            f"Error downloading {basename} from {candidate['user']}: {e}"
        )
    return None


def wait_for_completion(candidate, timeout=300):
    client = slskd_client.get_client()
    logging.debug(f"Waiting for transfer of {candidate['filename']} to complete...")
    transfer_id = None
    start = time.time()

    while time.time() - start < 10:
        downloads = Transfer.list_from_user_json(
            client.transfers.get_downloads(candidate["user"])
        )
        match = next(
            (
                t for t in downloads
                if t.id
                and t.basename == remote_basename(candidate["filename"])
                and t.size == candidate["size"]
            ),
            None,
        )
        if match:
            transfer_id = match.id
            break
        if slskd_client.interruptible_sleep(1):
            return None

    if not transfer_id:
        logging.error(f"Transfer ID not found for {candidate['filename']}")
        candidate["failure"] = "transfer not found in slskd"
        return None

    start = time.time()
    while True:
        downloads = Transfer.list_from_user_json(
            client.transfers.get_downloads(candidate["user"])
        )
        for transfer in downloads:
            if transfer.id != transfer_id:
                continue
            state = transfer.state.lower()
            logging.debug(f"State for {transfer.filename}: {state}")
            outcome = transfer.outcome
            if outcome is TransferOutcome.SUCCEEDED:
                filename = remote_basename(candidate["filename"])
                real_path = _await_completed_file(filename)
                logging.info(f"File found: {real_path}")
                if real_path and _wait_for_external_processing(real_path):
                    return real_path
                logging.warning(
                    f"File not confirmed after download: {filename}"
                )
                candidate["failure"] = "file not found after download"
                return None
            if outcome is TransferOutcome.FAILED:
                logging.warning(
                    f"Transfer failed: {transfer.filename} — state: {state}"
                )
                candidate["failure"] = f"transfer {state}"
                candidate["peer_fault"] = transfer.peer_fault
                return None
        if time.time() - start > timeout:
            logging.warning(f"Transfer timeout for {candidate['filename']}")
            candidate["failure"] = "transfer timed out"
            return None
        if slskd_client.interruptible_sleep(2):
            return None


def _wait_for_external_processing(file_path):
    if "/incomplete/" in file_path:
        logging.warning(f"Rejected incomplete path: {file_path}")
        return False

    start = time.time()
    while time.time() - start < config.EXTERNAL_PROCESS_WAIT_TIMEOUT:
        if os.path.exists(file_path):
            logging.info(f"File confirmed at: {file_path}")
            return True
        if slskd_client.interruptible_sleep(2):
            return False

    logging.warning(f"File did not appear within timeout: {file_path}")
    return False
