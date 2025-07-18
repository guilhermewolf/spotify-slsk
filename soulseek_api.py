import os
import time
import logging
import slskd_api
import shutil
import re
from rapidfuzz import fuzz
from db import get_tried_files, add_tried_file

DEFAULT_FORMATS = ".flac,.mp3,.aiff,.wav"

PREFERRED_FORMATS = [
    fmt_cleaned.lower()
    for fmt in os.getenv("SLSKD_PREFERRED_FORMATS", DEFAULT_FORMATS).split(",")
    if (fmt_cleaned := fmt.strip().strip('"').strip("'"))
]

DOWNLOAD_DIR = os.getenv("SLSKD_DOWNLOADS_DIR", "/downloads")
EXTERNAL_PROCESS_WAIT_TIMEOUT = int(os.getenv("SLSKD_WAIT_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("SLSKD_MAX_RETRIES", "2"))

slskd_host_url = os.getenv("SLSKD_HOST_URL", "http://slskd:5030")
slskd_api_key = os.getenv("SLSKD_API_KEY")
slskd_url_base = os.getenv("SLSKD_URL_BASE", "")
slskd_download_dir = os.getenv("SLSKD_DOWNLOAD_DIR", "/downloads")

slskd = slskd_api.SlskdClient(
    host=slskd_host_url,
    api_key=slskd_api_key,
    url_base=slskd_url_base,
)

def perform_search(artist, title, timeout=300):
    # Sanitize title and artist, removing special characters
    clean_title = re.sub(r'[^\w\s]', '', title).lower().strip()
    clean_artist = re.sub(r'[^\w\s]', '', artist).lower().strip()
    query = f"{clean_title} {clean_artist}"
    logging.info(f"Searching for: {query}")

    try:
        search = slskd.searches.search_text(
            searchText=query,
            filterResponses=False
        )
        start = time.time()
        while time.time() - start < timeout:
            state = slskd.searches.state(search["id"])["state"]
            if state != "InProgress":
                break
            time.sleep(1)
        else:
            logging.warning(f"Search timed out for: {query}")
            return []

        results = slskd.searches.search_responses(search["id"])
        logging.info(f"Search returned {len(results)} results for: {query}")
        return results

    except Exception as e:
        logging.error(f"Search failed for '{query}': {e}")
        return []
def slskd_version_check(version, target="0.22.2"):
    version_tuple = tuple(map(int, version.split(".")[:3]))
    target_tuple = tuple(map(int, target.split(".")[:3]))
    return version_tuple > target_tuple

def cancel_and_delete(delete_dir, username, files):
    for file in files:
        try:
            slskd.transfers.cancel_download(username=username, id=file["id"])
        except Exception as e:
            logging.warning(f"Failed to cancel transfer: {file['id']} from {username}: {e}")

    if os.path.exists(delete_dir):
        try:
            shutil.rmtree(delete_dir)
            logging.info(f"Deleted directory: {delete_dir}")
            
        except Exception as e:
            logging.warning(f"Could not delete {delete_dir}: {e}")

def clean_filename(filename):
    """
    Clean a filename by removing common tags, normalizing spaces, and removing the extension.
    
    Args:
        filename (str): The raw filename from Soulseek search results.
    
    Returns:
        str: The cleaned filename in lowercase, without tags or extension.
    """
    # Remove text within brackets and parentheses (e.g., [FLAC], (2013))
    filename = re.sub(r'\[.*?\]', '', filename)
    filename = re.sub(r'\(.*?\)', '', filename)
    # Remove common metadata tags (e.g., 24bit, 44.1kHz)
    filename = re.sub(r'\b\d{1,2}bit\b|\b\d{1,3}\.\d{1,2}kHz\b|\b\d{4}\b', '', filename, flags=re.IGNORECASE)
    # Remove file extension
    filename = os.path.splitext(filename)[0]
    # Replace underscores and hyphens with spaces
    filename = filename.replace("_", " ").replace("-", " ")
    # Normalize multiple spaces to a single space
    filename = ' '.join(filename.split())
    return filename.lower().strip()

def extract_candidates(search_results, expected_title, expected_artist, min_title_score=85, min_artist_score=75):
    """
    Extract valid file candidates from Soulseek search results based on title and artist matching.
    
    Args:
        search_results (list): List of search result dictionaries from slskd API.
        expected_title (str): The expected track title from Spotify.
        expected_artist (str): The expected artist(s) from Spotify, comma-separated.
        min_title_score (float): Minimum fuzzy matching score for title (default: 85).
        min_artist_score (float): Minimum fuzzy matching score for artist (default: 75).
    
    Returns:
        list: List of candidate dictionaries with user, filename, size, bitrate, extension, and scores.
    """
    candidates = []
    # Clean expected title
    expected_title_clean = ' '.join(expected_title.lower().split())
    # Split and clean expected artists
    expected_artists = [artist.strip().lower() for artist in expected_artist.split(',')]
    
    logging.debug(f"Search results received: {len(search_results)} total users")
    logging.debug(f"Expected title: {expected_title_clean}")
    logging.debug(f"Expected artists: {expected_artists}")
    
    for result in search_results:
        user = result.get("username", "unknown")
        files = result.get("files", [])
        logging.debug(f"User: {user} has {len(files)} files")
        
        for file in files:
            filename = file.get("filename")
            if not filename:
                logging.debug(f"Skipping file: No filename in {file}")
                continue
                
            ext = os.path.splitext(filename)[1].lower()
            if ext not in PREFERRED_FORMATS:
                logging.debug(f"Skipping {filename}: unsupported format ({ext})")
                continue
                
            # Filter MP3s that are not 320 kbps
            bitrate = file.get("bitrate")
            if ext == ".mp3" and bitrate != 320:
                logging.debug(f"Skipped {filename} — MP3 with bitrate {bitrate} kbps")
                continue
                
            base = os.path.basename(filename)
            clean_base = clean_filename(base)
            logging.debug(f"Cleaned filename: {clean_base}")
            
            # Compute title score
            title_score = fuzz.partial_ratio(expected_title_clean, clean_base)
            # Compute artist scores and take the maximum
            artist_scores = [fuzz.partial_ratio(artist, clean_base) for artist in expected_artists]
            max_artist_score = max(artist_scores) if artist_scores else 0
            
            logging.debug(f"Scores for {base} - Title: {title_score:.2f}, Max Artist: {max_artist_score:.2f}")
            
            if title_score >= min_title_score and max_artist_score >= min_artist_score:
                logging.debug(f"Accepted: {base} (title_score: {title_score:.2f}, artist_score: {max_artist_score:.2f})")
                candidates.append({
                    "user": user,
                    "filename": base,
                    "size": file.get("size"),
                    "bitrate": bitrate,
                    "ext": ext,
                    "title_score": title_score,
                    "artist_score": max_artist_score,
                })
            else:
                logging.debug(f"Rejected: {base} (title_score: {title_score:.2f}, artist_score: {max_artist_score:.2f})")
    
    logging.debug(f"Final candidates count: {len(candidates)}")
    return candidates

def sort_candidates(candidates):
    return sorted(
        candidates,
        key=lambda c: PREFERRED_FORMATS.index(c["ext"])
        if c["ext"] in PREFERRED_FORMATS
        else len(PREFERRED_FORMATS),
    )

def find_file_in_downloads(filename, base_dir="/downloads"):
    for root, _, files in os.walk(base_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None

def download_and_verify(search_results, expected_title, expected_artist, conn, playlist_name, track_id, max_attempts=2):
    candidates = extract_candidates(search_results, expected_title, expected_artist)
    if not candidates:
        logging.warning("No valid candidates found.")
        return None

    sorted_candidates = sort_candidates(candidates)
    tried_filenames = set(get_tried_files(conn, playlist_name, track_id))

    for candidate in sorted_candidates:
        basename = os.path.basename(candidate['filename'])

        if basename in tried_filenames:
            logging.info(f"Skipping previously tried file: {basename}")
            continue

        logging.info(f"Attempting download: {candidate['filename']} from {candidate['user']}")
        try:
            slskd.transfers.enqueue(
                username=candidate['user'],
                files=[{
                    "filename": candidate['filename'],
                    "size": candidate['size']
                }]
            )

            file_path = wait_for_completion(candidate)
            if file_path:
                logging.info(f"✅ Downloaded and verified: {file_path}")

                if not _wait_for_external_processing(file_path):
                    logging.warning(f"❌ Post-download verification failed for: {basename}")
                    add_tried_file(conn, playlist_name, track_id, basename)
                    continue

                return file_path
            else:
                logging.warning(f"❌ Download failed or was not confirmed: {basename}")
                add_tried_file(conn, playlist_name, track_id, basename)

        except Exception as e:
            logging.error(f"Error downloading {basename} from {candidate['user']}: {e}")
            add_tried_file(conn, playlist_name, track_id, basename)

    logging.warning("Exhausted all download attempts.")
    return None


def wait_for_completion(candidate, timeout=300):
    logging.debug(f"Waiting for transfer of {candidate['filename']} to complete...")
    transfer_id = None
    start = time.time()

    # First, locate the transfer ID
    while time.time() - start < 10:
        downloads = slskd.transfers.get_downloads(candidate["user"])
        for directory in downloads.get("directories", []):
            for file in directory.get("files", []):
                if (
                    os.path.basename(file["filename"]) == os.path.basename(candidate["filename"]) and
                    file["size"] == candidate["size"]
                ):
                    transfer_id = file["id"]
                    break
            if transfer_id:
                break
        if transfer_id:
            break
        time.sleep(1)

    if not transfer_id:
        logging.error(f"Transfer ID not found for {candidate['filename']}")
        return None

    # Monitor transfer state
    start = time.time()
    while True:
        downloads = slskd.transfers.get_downloads(candidate["user"])
        for directory in downloads.get("directories", []):
            for file in directory.get("files", []):
                if file["id"] == transfer_id:
                    state = file.get("state", "").lower()
                    logging.debug(f"State for {file['filename']}: {state}")
                    if "completed" in state and "succeeded" in state:
                        filename = os.path.basename(candidate["filename"].replace("\\", "/"))
                        real_path = find_file_in_downloads(filename, base_dir=DOWNLOAD_DIR)
                        logging.info(f"File found: {real_path}")
                        if real_path and _wait_for_external_processing(real_path):
                            return real_path
                        else:
                            logging.warning(f"File not confirmed after download: {filename}")
                            return None
                    elif any(word in state for word in ["failed", "aborted", "errored"]):
                        logging.warning(f"Transfer failed: {file['filename']} — state: {state}")
                        return None
        if time.time() - start > timeout:
            logging.warning(f"Transfer timeout for {candidate['filename']}")
            return None
        time.sleep(2)


def _wait_for_external_processing(file_path):
    if "/incomplete/" in file_path:
        logging.warning(f"Rejected incomplete path: {file_path}")
        return False

    start = time.time()
    while time.time() - start < EXTERNAL_PROCESS_WAIT_TIMEOUT:
        if os.path.exists(file_path):
            logging.info(f"File confirmed at: {file_path}")
            return True
        time.sleep(2)

    logging.warning(f"File did not appear within timeout: {file_path}")
    return False
