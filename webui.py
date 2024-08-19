from flask import Flask, render_template
import sqlite3

app = Flask(__name__)

def get_db_connection():
    conn = sqlite3.connect('/app/data/playlist_tracks.db')
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    conn = get_db_connection()
    playlists = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    data = {}

    for playlist in playlists:
        table_name = playlist['name']
        tracks = conn.execute(f"SELECT * FROM {table_name};").fetchall()
        data[table_name] = tracks

    conn.close()
    return render_template('index.html', data=data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5110)
