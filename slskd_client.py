import logging
import re
import difflib
import time
import os
from slskd_api import SlskdClient as BaseSlskdClient

logger = logging.getLogger(__name__)

class SlskdClient(BaseSlskdClient):
    def __init__(self, host, api_key, url_base=""):
        super().__init__(host=host, api_key=api_key, url_base=url_base)

    def download_from_search(self, artist, title, playlist_name):
        query = f"{artist} {title}"
        logger.info(f"Searching for: {query}")

        try:
            # Start the search
            search = self.searches.search_text(searchText=query)
            search_id = search['id']

            # Wait until search is finished (blocking)
            while self.searches.state(search_id)['state'] == 'InProgress':
                time.sleep(0.5)

            results = self.searches.search_responses(search_id)
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
            return None

        if not results:
            logger.warning(f"No results found for: {query}")
            return None

        expected = f"{artist} - {title}".lower()
        best_match = None

        for result in results:
            for file in result.get('files', []):
                filename = file['filename'].lower()
                if expected in filename or title.lower() in filename:
                    best_match = {
                        'user': result['username'],
                        'file': file['filename'],
                        'size': file.get('size')
                    }
                    break
            if best_match:
                break

        if not best_match:
            logger.warning(f"No good match found for: {query}")
            return None

        try:
            self.transfers.enqueue(
                username=best_match['user'],
                files=[{
                    "filename": best_match['file'],
                    "size": best_match['size']
                }]
            )
            logger.info(f"Download started for: {artist} - {title} from {best_match['user']}")
            # Return the raw downloaded file path for processing
            return os.path.join("/downloads", best_match["file"])
        except Exception as e:
            logger.error(f"Download failed for {artist} - {title} from {best_match['user']}: {e}")
            return None
