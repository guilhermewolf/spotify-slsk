import logging
import os
import time
from slskd_api.client import SlskdClient as BaseSlskdClient

logger = logging.getLogger(__name__)

PREFERRED_FORMATS = [
    fmt.strip().lower() for fmt in os.getenv("SLSKD_PREFERRED_FORMATS", ".flac,.mp3,.aiff,.wav").split(",")
]

class SlskdClient(BaseSlskdClient):
    def __init__(self, host, api_key, url_base=""):
        super().__init__(host=host, api_key=api_key, url_base=url_base)

    def perform_search(self, artist, title):
        query = f"{artist} {title}"
        logger.info(f"Searching for: {query}")

        try:
            search = self.searches.search_text(searchText=query)
            search_id = search['id']

            while self.searches.state(search_id)['state'] == 'InProgress':
                time.sleep(1)

            results = self.searches.search_responses(search_id)
            logger.info(f"Search returned {len(results)} results for: {query}")
            return results
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
            return []

    def download_best_candidate(self, search_results, exclude_first=False):
        candidates = []

        for result in search_results:
            user = result['username']
            for file in result.get('files', []):
                filename = file['filename']
                ext = os.path.splitext(filename)[1].lower()
                if any(fmt in ext for fmt in PREFERRED_FORMATS):
                    candidates.append({
                        'user': user,
                        'filename': filename,
                        'size': file.get('size'),
                        'ext': ext
                    })

        if not candidates:
            logger.warning("No valid candidates found.")
            return None

        sorted_candidates = sorted(
            candidates,
            key=lambda c: PREFERRED_FORMATS.index(c['ext']) if c['ext'] in PREFERRED_FORMATS else len(PREFERRED_FORMATS)
        )

        if exclude_first:
            sorted_candidates = sorted_candidates[1:]

        for candidate in sorted_candidates:
            try:
                logger.info(f"Enqueuing {candidate['filename']} from {candidate['user']}")
                transfer = self.transfers.enqueue(
                    username=candidate['user'],
                    files=[{
                        "filename": candidate['filename'],
                        "size": candidate['size']
                    }]
                )
                transfer_id = transfer["id"]

                while True:
                    state = self.transfers.state(transfer_id)["state"]
                    if state == "Completed":
                        logger.info(f"Download completed: {candidate['filename']}")
                        return os.path.join("/downloads", candidate["filename"])
                    elif state in ("Failed", "Aborted", "Rejected"):
                        logger.warning(f"Download failed or aborted for {candidate['filename']}")
                        break
                    time.sleep(2)

            except Exception as e:
                logger.error(f"Failed to download from {candidate['user']}: {e}")

        return None
