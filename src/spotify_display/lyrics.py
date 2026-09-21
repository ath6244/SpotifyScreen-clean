"""LRCLIB lyrics. Optional — the display works fine without it.

LRCLIB's /api/get is an exact lookup: track, artist, album and duration all
have to line up with whatever the contributor typed. Spotify's metadata
frequently does not, so a single exact call misses songs whose lyrics are
sitting right there in the database. This module escalates through
progressively looser lookups and falls back to the fuzzy /api/search, logging
which rung actually worked so the ladder can be tuned against real misses.
"""

import hashlib
import json
import os
import re
import time

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(PROJECT_ROOT, "var", "cache")
API_GET = "https://lrclib.net/api/get"
API_SEARCH = "https://lrclib.net/api/search"
LINE = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]\s*(.*)")
TIMEOUT = 4  # lyrics are the last thing to arrive and the least missed

# A miss is not permanent. Lyrics get contributed to LRCLIB every day, and the
# old code wrote [] to the cache forever — one failed lookup and that song was
# never tried again for the life of the machine.
MISS_TTL = 24 * 3600

# How far a /api/search result's duration may be from Spotify's before it is
# considered a different recording. Live versions and remasters drift by a
# second or two; a different arrangement drifts by much more.
DURATION_TOLERANCE = 5

USER_AGENT = "spotify-display/0.1 (local desk display)"

# " - 2011 Remaster", " - Radio Edit", " - Live at Wembley"
SUFFIX = re.compile(r"\s+-\s+.*$")
# "(Remastered)", "(Deluxe Edition)", "[Bonus Track]"
QUALIFIER = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def fetch(track):
    """Return {"lines": [[ms, text], ...], "synced": bool}.

    Parsed on the laptop so the phone receives plain data. Gingerbread's JS
    engine should never see a regex loop over a few hundred lines.
    """
    if not track or not track.get("title"):
        return _empty()

    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha1(
        ("%s|%s|%s" % (track["title"], track["artist"], track["album"])).encode("utf-8")
    ).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, "lrc-%s.json" % key)

    cached = _read_cache(path)
    if cached is not None:
        return cached

    result, reachable = _lookup(track)

    # Only record a miss when LRCLIB actually answered. A network failure is
    # not evidence that the lyrics do not exist, and caching it as one would
    # suppress the retry for a day.
    if result["lines"] or reachable:
        _write_cache(path, result)

    return result


def _empty():
    return {"lines": [], "synced": False}


# ---- cache --------------------------------------------------------------

def _read_cache(path):
    try:
        with open(path) as fh:
            blob = json.load(fh)
    except (IOError, ValueError):
        return None

    # Files written before this module carried a flag: a bare list of lines.
    if isinstance(blob, list):
        if not blob:
            return None            # a legacy permanent miss — retry it
        return {"lines": blob, "synced": True}

    if blob.get("lines"):
        return {"lines": blob["lines"], "synced": bool(blob.get("synced"))}

    # A cached miss, good for MISS_TTL and no longer.
    if time.time() - blob.get("ts", 0) < MISS_TTL:
        return _empty()
    return None


def _write_cache(path, result):
    blob = {
        "ts": int(time.time()),
        "synced": result["synced"],
        "lines": result["lines"],
    }
    try:
        with open(path, "w") as fh:
            json.dump(blob, fh)
    except IOError as exc:
        print("[lyrics] cache write failed: %s" % exc)


# ---- lookup -------------------------------------------------------------

def _clean_title(title):
    """Strip the qualifiers Spotify appends and LRCLIB usually does not have.

    "Karma Police - 2011 Remaster" -> "Karma Police"
    "Everything In Its Right Place (Remastered)" -> "Everything In Its Right Place"
    """
    cleaned = SUFFIX.sub("", title)
    cleaned = QUALIFIER.sub("", cleaned)
    cleaned = cleaned.strip()

    # If stripping left almost nothing, the dash was part of the title rather
    # than a qualifier ("- - -", "Go - The Extended Version"). Search on a
    # two-character stub matches everything and means nothing.
    if len(cleaned) < 3:
        return title
    return cleaned


def _lookup(track):
    """Walk the ladder. Returns (result, lrclib_answered_at_least_once)."""
    title = track["title"]
    artist = (track.get("artist") or "").strip()
    first_artist = artist.split(",")[0].strip()
    album = track.get("album") or ""
    duration = int(round((track.get("duration_ms") or 0) / 1000.0))
    clean = _clean_title(title)

    # Loosest constraint last. Album goes first because remasters and deluxe
    # editions break it constantly; duration goes next because LRCLIB rejects
    # a get outright when it is more than a couple of seconds out.
    steps = [
        ("exact", {"track_name": title, "artist_name": artist,
                   "album_name": album, "duration": duration}),
        ("no-album", {"track_name": title, "artist_name": artist,
                      "duration": duration}),
        ("first-artist", {"track_name": title, "artist_name": first_artist}),
        ("clean-title", {"track_name": clean, "artist_name": first_artist}),
    ]

    reachable = False
    for label, params in steps:
        if not params.get("artist_name"):
            continue
        payload, answered = _request(API_GET, params)
        reachable = reachable or answered
        result = _from_payload(payload)
        if result["lines"]:
            print("[lyrics] %s — %s via get:%s%s"
                  % (title, artist, label, "" if result["synced"] else " (unsynced)"))
            return result, reachable

    # Fuzzy, and the only step that can match when the metadata disagrees on
    # more than one field at once.
    result, answered = _search(title, clean, first_artist, duration)
    reachable = reachable or answered
    if result["lines"]:
        print("[lyrics] %s — %s via search%s"
              % (title, artist, "" if result["synced"] else " (unsynced)"))
        return result, reachable

    print("[lyrics] %s — %s: no match (retry in %dh)"
          % (title, artist, MISS_TTL // 3600))
    return _empty(), reachable


def _search(title, clean, artist, duration):
    """Fuzzy search, closest duration wins. Returns (result, answered)."""
    params = {"track_name": clean or title}
    if artist:
        params["artist_name"] = artist

    payload, answered = _request(API_SEARCH, params)
    if not isinstance(payload, list) or not payload:
        return _empty(), answered

    def distance(entry):
        try:
            return abs(float(entry.get("duration") or 0) - duration)
        except (TypeError, ValueError):
            return 1e9

    # Prefer synced over plain at equal closeness, and never accept a
    # recording whose length is nothing like the one that is playing.
    candidates = [e for e in payload if distance(e) <= DURATION_TOLERANCE]
    if not candidates:
        return _empty(), answered

    candidates.sort(key=lambda e: (0 if e.get("syncedLyrics") else 1, distance(e)))
    return _from_payload(candidates[0]), answered


def _request(url, params):
    """Returns (decoded json or None, whether LRCLIB answered at all)."""
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        print("[lyrics] lookup failed: %s" % exc)
        return None, False

    if resp.status_code == 404:
        return None, True          # answered, and the answer is "no"
    if resp.status_code != 200:
        return None, False

    try:
        return resp.json(), True
    except ValueError:
        return None, True


def _from_payload(payload):
    """Prefer synced lyrics; keep plain ones flagged rather than dropping them."""
    if not isinstance(payload, dict):
        return _empty()

    synced = payload.get("syncedLyrics")
    if synced:
        lines = _parse(synced)
        if lines:
            return {"lines": lines, "synced": True}

    plain = payload.get("plainLyrics")
    if plain:
        # No timestamps exist for these, and inventing them by spreading the
        # lines evenly over the duration would be a guess presented as data.
        # They travel with synced=False so the frontend can decide.
        lines = [[None, text.strip()] for text in plain.splitlines() if text.strip()]
        if lines:
            return {"lines": lines, "synced": False}

    return _empty()


def _parse(lrc):
    out = []
    for raw in lrc.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        minutes, seconds, frac, text = m.groups()
        ms = int(minutes) * 60000 + int(seconds) * 1000
        if frac:
            ms += int(frac.ljust(3, "0")[:3])
        if text:
            out.append([ms, text])
    return sorted(out, key=lambda x: x[0])
