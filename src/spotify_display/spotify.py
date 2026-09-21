"""Spotify integration. Auth happens on the laptop, never on the phone."""

import os

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Changing this list invalidates the cached token: the refresh token carries
# the scopes it was granted with, so var/spotify-token has to be deleted and
# consent given again. user-modify-playback-state is what the transport
# buttons need.
SCOPE = ("user-read-currently-playing user-read-playback-state "
         "user-modify-playback-state")


class PollFailed(Exception):
    """The poll did not complete — network down, token refresh failed, rate
    limit, anything.

    Distinct from now_playing() returning None, which means the poll worked
    and Spotify says nothing is playing. Conflating the two publishes "idle"
    to the phone on every network blip, and on every wake from suspend, which
    is a lie: we do not know what is playing, we could not ask.
    """


class NoActiveDevice(Exception):
    """Spotify has no device to send the command to.

    Not an error in this system — Spotify simply forgets the active device
    after a while idle, and the first command after that fails until
    something is played on a real client again.
    """

# Anchored to the project root, not the working directory. A relative cache
# path silently re-runs the whole OAuth flow whenever the server is started
# from somewhere other than the repo root.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CACHE = os.path.join(PROJECT_ROOT, "var", "spotify-token")


def make_client():
    """Build an authenticated Spotipy client.

    The OAuth redirect opens a browser window on the LAPTOP. The phone is
    never part of the auth flow — its browser cannot complete a modern TLS
    handshake with accounts.spotify.com.

    Note the redirect URI must be the explicit loopback IP. Spotify rejects
    `localhost` outright, and does it as a redirect-uri mismatch rather than
    a message that names the real problem.
    """
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIPY_CLIENT_ID"],
        client_secret=os.environ["SPOTIPY_CLIENT_SECRET"],
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=SCOPE,
        cache_path=os.environ.get("SPOTIPY_CACHE", DEFAULT_CACHE),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=8, retries=2)


def _command(call):
    """Run a playback command, translating the one failure that is routine.

    Everything spotipy-shaped stays in this module: state.py and server.py
    only ever see NoActiveDevice or a generic exception.
    """
    try:
        call()
    except spotipy.SpotifyException as exc:
        if exc.http_status == 404 or "NO_ACTIVE_DEVICE" in str(exc):
            raise NoActiveDevice(
                "Spotify has no active device. Play something on a real "
                "client once and it will come back."
            )
        raise


def play(client):
    _command(client.start_playback)


def pause(client):
    _command(client.pause_playback)


def skip(client):
    _command(client.next_track)


def now_playing(client):
    """Return a normalized track dict, or None when nothing is loaded.

    Shape is deliberately flat and stable: the frontend depends on it, and
    a future non-Spotify source only has to emit the same keys.

    This calls GET /v1/me/player (not /me/player/currently-playing) because
    it also reports which device is active. Both survived the February 2026
    API changes; neither returns `popularity` any more, which nothing here
    ever asked for.
    """
    try:
        # An expired access token refreshes transparently here: spotipy's
        # validate_token() checks expiry before every call and swaps in a new
        # one from the refresh token, which does not expire. After an
        # overnight suspend that refresh is the first thing that happens — and
        # the first thing that fails if the network is not up yet, with a
        # ConnectionError that spotipy's own `except HTTPError` does not catch.
        data = client.current_playback(additional_types="track,episode")
    except Exception as exc:  # network blip, token refresh, rate limit
        raise PollFailed(str(exc))

    if not data or not data.get("item"):
        return None

    item = data["item"]
    images = []
    if item.get("type") == "episode":
        title = item.get("name", "")
        artist = (item.get("show") or {}).get("name", "")
        images = (item.get("show") or {}).get("images") or item.get("images") or []
    else:
        title = item.get("name", "")
        artist = ", ".join(a["name"] for a in item.get("artists", []))
        images = (item.get("album") or {}).get("images", [])

    # Spotify returns images largest-first. The phone is 320px wide, so the
    # smallest image that is still >= 300px is plenty.
    art_url = None
    if images:
        usable = [i for i in images if (i.get("width") or 0) >= 300]
        art_url = (usable[-1] if usable else images[0])["url"]

    return {
        "id": item.get("id") or item.get("uri") or title,
        "title": title,
        "artist": artist,
        "album": (item.get("album") or {}).get("name", ""),
        "duration_ms": item.get("duration_ms") or 0,
        "progress_ms": data.get("progress_ms") or 0,
        "is_playing": bool(data.get("is_playing")),
        "art_url": art_url,
    }
