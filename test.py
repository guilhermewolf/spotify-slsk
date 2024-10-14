import asyncio
import os
import logging
from aioslsk import SoulseekClient

async def test_soulseek():
    # Setup basic logging
    logging.basicConfig(level=logging.INFO)

    # Fetch credentials from environment variables
    SLSK_USER = os.getenv('SLSK_USER')
    SLSK_PASS = os.getenv('SLSK_PASS')

    # Check if credentials are set
    if not SLSK_USER or not SLSK_PASS:
        logging.error("Soulseek credentials are missing!")
        return

    async with SoulseekClient() as client:
        try:
            # Connect to Soulseek
            await client.connect()
            await client.login(SLSK_USER, SLSK_PASS)
            logging.info(f"Logged into Soulseek as {SLSK_USER}")

            # Search for a simple track (replace with any song you want)
            search_query = "Daft Punk Get Lucky"
            search_results = await client.search(search_query)

            # Check if any results were returned
            if search_results:
                logging.info(f"Found {len(search_results)} results for '{search_query}'")
                logging.info(f"First result: {search_results[0].file}")
            else:
                logging.warning(f"No results found for '{search_query}'")
        
        except Exception as e:
            logging.error(f"Error during Soulseek test: {e}")

if __name__ == "__main__":
    asyncio.run(test_soulseek())
