import os

import spotipy
from spotipy.oauth2 import SpotifyOAuth

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.environ["SPOTIPY_CLIENT_ID"],
    client_secret=os.environ["SPOTIPY_CLIENT_SECRET"],
    redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
    scope="user-read-currently-playing user-read-playback-state",
    cache_path=os.environ.get("SPOTIPY_CACHE", "var/spotify-token"),
))

data = sp.current_playback()
if not data or not data.get("item"):
    print("Connected, but nothing playing. Start a track and rerun.")
else:
    print("OK:", data["item"]["name"], "-", data["item"]["artists"][0]["name"])
    print("progress:", data["progress_ms"], "of", data["item"]["duration_ms"])