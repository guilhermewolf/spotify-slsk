import musicbrainzngs
import os
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC

# Configure MusicBrainz
musicbrainzngs.set_useragent(
    "Application",  # Replace with your app's name
    "1.0",          # Replace with your app's version
    "your_email@example.com"  # Replace with your contact email
)

def fetch_metadata_from_musicbrainz(track_name, artist_name):
    try:
        result = musicbrainzngs.search_recordings(query=track_name, artist=artist_name, limit=1)
        if result['recording-list']:
            recording = result['recording-list'][0]
            title = recording['title']
            artist = recording['artist-credit'][0]['name']
            album = recording['release-list'][0]['title'] if 'release-list' in recording else None
            date = recording['release-list'][0]['date'] if 'release-list' in recording and 'date' in recording['release-list'][0] else None
            return {'title': title, 'artist': artist, 'album': album, 'date': date}
        else:
            return None
    except musicbrainzngs.WebServiceError as e:
        logging.error(f"MusicBrainz API error: {e}")
        return None

def update_track_metadata(file_path, metadata):
    audio = MP3(file_path, ID3=ID3)
    
    audio["TIT2"] = TIT2(encoding=3, text=metadata['title'])
    audio["TPE1"] = TPE1(encoding=3, text=metadata['artist'])
    if metadata['album']:
        audio["TALB"] = TALB(encoding=3, text=metadata['album'])
    if metadata['date']:
        audio["TDRC"] = TDRC(encoding=3, text=metadata['date'])
    
    audio.save()

def process_downloaded_tracks(playlist_name):
    download_path = f"/app/data/downloads/{playlist_name}"
    
    for root, dirs, files in os.walk(download_path):
        for file in files:
            if file.endswith(".mp3"):
                file_path = os.path.join(root, file)
                track_name, artist_name = extract_basic_metadata(file)
                
                metadata = fetch_metadata_from_musicbrainz(track_name, artist_name)
                if metadata:
                    update_track_metadata(file_path, metadata)
                else:
                    logging.warning(f"Could not find metadata for {file_path}")

def extract_basic_metadata(filename):
    # Implement basic parsing of filename to get track name and artist
    # Example: "Artist - Track.mp3" -> ("Track", "Artist")
    parts = filename.rsplit("-", 1)
    if len(parts) == 2:
        artist = parts[0].strip()
        track = parts[1].replace(".mp3", "").strip()
        return track, artist
    return None, None