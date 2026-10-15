class Track:
    def __init__(self, id, name, artist, album, playlist_id, duration_ms=None):
        self.id = id
        self.name = name
        self.artist = artist
        self.album = album
        self.playlist_id = playlist_id
        self.duration_ms = duration_ms

    def __repr__(self):
        return f"Track(id={self.id}, name={self.name}, artist={self.artist}, album={self.album})"
