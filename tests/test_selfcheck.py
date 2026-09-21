"""Self-check for the publish/merge logic. No network, no Spotify, no phone.

Run: python -m tests.test_selfcheck

Covers the parts that are easy to get wrong and hard to debug on the phone:
the version counter never being falsy, staged publishes not rewinding the
progress bar, and stale enrichment workers being dropped after a skip.
"""

import json
import os
import sys
import time

from spotify_display import art, lyrics, server, spotify, state


def _track(store, track_id, progress=1000, playing=True, duration=200000):
    """Push a payload through the store the way _tick would."""
    payload = {
        "status": "playing" if playing else "paused",
        "id": track_id,
        "title": "T", "artist": "A", "album": "Al",
        "duration_ms": duration,
        "progress_ms": progress,
        "is_playing": playing,
        "art": None,
        "lyrics": [],
    }
    store._publish(payload)
    store._last_track_id = track_id


def check_version_never_falsy():
    store = state.Store(enable_lyrics=False)
    version, payload = store.snapshot()
    assert version == 1, "initial version must be 1, got %r" % version
    assert payload["version"] == 1, "initial payload must carry a version"
    # 0 is falsy in JS; the client would pin at v=0 and busy-loop.
    assert payload["version"], "initial version must be truthy in JS terms"

    _track(store, "a")
    version, _ = store.snapshot()
    assert version == 2, "publish must bump the version, got %r" % version


def check_merge_does_not_rewind_progress():
    store = state.Store(enable_lyrics=False)
    _track(store, "a", progress=10000, playing=True)

    # Simulate art landing 4s later, the way the enrichment worker does.
    with store._lock:
        store._payload["server_time"] -= 4000

    store._merge("a", {"art": {"cover": "/art/x.jpg"}})
    _, payload = store.snapshot()

    assert payload["art"] is not None, "merge must attach the art"
    assert payload["progress_ms"] >= 13900, (
        "merge must roll progress forward, got %r (bar would jump backwards)"
        % payload["progress_ms"]
    )


def check_merge_does_not_overrun_duration():
    store = state.Store(enable_lyrics=False)
    _track(store, "a", progress=199000, playing=True, duration=200000)
    with store._lock:
        store._payload["server_time"] -= 30000

    store._merge("a", {"art": None})
    _, payload = store.snapshot()
    assert payload["progress_ms"] <= 200000, (
        "progress must clamp to duration, got %r" % payload["progress_ms"]
    )


def check_merge_drops_stale_worker():
    store = state.Store(enable_lyrics=False)
    _track(store, "a")
    _track(store, "b")            # user skipped while art for "a" was fetching
    before, _ = store.snapshot()

    store._merge("a", {"art": {"cover": "/art/stale.jpg"}})
    after, payload = store.snapshot()

    assert after == before, "stale merge must not publish a new version"
    assert payload["id"] == "b", "stale merge must not overwrite the new track"
    assert payload["art"] is None, "art from the old track must not leak through"


def check_paused_merge_does_not_advance():
    store = state.Store(enable_lyrics=False)
    _track(store, "a", progress=5000, playing=False)
    with store._lock:
        store._payload["server_time"] -= 10000

    store._merge("a", {"art": None})
    _, payload = store.snapshot()
    assert payload["progress_ms"] == 5000, (
        "paused track must not advance, got %r" % payload["progress_ms"]
    )


def check_should_publish_suppresses_noops():
    store = state.Store(enable_lyrics=False)
    _track(store, "a", progress=10000, playing=True)

    # Same track, progress exactly where interpolation predicts it.
    time.sleep(0.05)
    _, prev = store.snapshot()
    elapsed = (time.time() * 1000) - prev["server_time"]
    steady = dict(prev)
    steady["progress_ms"] = int(prev["progress_ms"] + elapsed)
    assert not store._should_publish(steady), "steady playback must not republish"

    seek = dict(prev)
    seek["progress_ms"] = prev["progress_ms"] + 30000
    assert store._should_publish(seek), "a seek must republish"

    paused = dict(prev)
    paused["is_playing"] = False
    assert store._should_publish(paused), "a pause must republish"


def check_waiter_wakes_on_publish():
    store = state.Store(enable_lyrics=False)
    version, _ = store.snapshot()

    import threading
    result = {}

    def waiter():
        result["version"], result["payload"] = store.wait_for_change(version, timeout=5.0)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.1)
    _track(store, "a")
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "waiter did not wake — long poll would hang"
    assert result["version"] > version, "waiter must observe the new version"


def check_future_version_does_not_block():
    """A client that outlived the server must not park on every poll.

    The server restarts at version 1; the client still asks for the version it
    had before. Blocking there costs the full 25s timeout per poll.
    """
    _track(server.store, "a")                 # server is now at version 2
    client = server.app.test_client()

    started = time.time()
    response = client.get("/api/state?v=19")  # ahead of anything we issued
    elapsed = time.time() - started

    assert elapsed < 1.0, "future version blocked for %.1fs" % elapsed
    body = response.get_json()
    assert body["version"] == 2, (
        "must answer with the real version so the client re-syncs, got %r"
        % body["version"]
    )

    # A version behind the current one must still return at once.
    started = time.time()
    response = client.get("/api/state?v=1")
    assert time.time() - started < 1.0, "stale version must return immediately"
    assert response.get_json()["version"] == 2


def check_auth_retries_until_success():
    """Cold boot has no DNS for a while. Auth must wait it out, not exit."""
    class FakeTime(object):
        def __init__(self):
            self.slept = []

        def sleep(self, seconds):
            self.slept.append(seconds)

    fake = FakeTime()
    real_time, real_auth = server.time, server.store.authenticate
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise IOError("getaddrinfo failed")

    server.time, server.store.authenticate = fake, flaky
    try:
        server._authenticate()          # must return, never SystemExit
    finally:
        server.time, server.store.authenticate = real_time, real_auth

    assert attempts["n"] == 4, "must keep retrying, stopped after %d" % attempts["n"]
    assert fake.slept == [5, 10, 20], "backoff must double, got %r" % fake.slept

    # And the doubling has to stop somewhere.
    assert server.AUTH_MAX_RETRY_SECONDS == 30


def check_resume_from_suspend():
    """A wake from overnight suspend must retry fast and force one publish.

    The laptop sleeps freely. On resume the poll thread's 3s wait returns
    after hours of wall clock and the first polls fail while Wi-Fi and DNS
    come back. That is not Spotify being down, and it must not be handled
    like it. Drives the real run_forever loop with a fake clock.
    """
    store = state.Store(enable_lyrics=False)
    store._client = object()
    # Enrichment publishes a merge of its own, which would satisfy the version
    # assertion below whether or not the force flag works. Off, so the only
    # thing that can bump the version is the tick under test.
    store._start_enrichment = lambda track: None

    intervals = []

    # One normal poll, then the machine suspends. The two polls straight after
    # the resume fail on DNS, the third works — the real shape of a wake.
    outcomes = ["ok", "fail", "fail", "ok"]
    SUSPEND_BEFORE = 1          # the wait before outcomes[1] reports hours
    step = {"n": 0}
    track = {
        "id": "a", "title": "T", "artist": "A", "album": "Al",
        "duration_ms": 200000, "progress_ms": 1000, "is_playing": True,
        "art_url": None,
    }

    class Stop(Exception):
        pass

    def fake_sleep(seconds):
        intervals.append(seconds)
        if step["n"] >= len(outcomes):
            raise Stop()
        # Report the wall clock a resume would show: hours, not seconds.
        return 6 * 3600.0 if step["n"] == SUSPEND_BEFORE else seconds

    def fake_now_playing(client):
        outcome = outcomes[step["n"]]
        step["n"] += 1
        if outcome == "fail":
            raise spotify.PollFailed("getaddrinfo failed")
        return dict(track)

    real_sleep, real_now = store._sleep, spotify.now_playing
    store._sleep, spotify.now_playing = fake_sleep, fake_now_playing
    try:
        store.run_forever()
    except Stop:
        pass
    finally:
        store._sleep, spotify.now_playing = real_sleep, real_now

    # The waits following the two failed polls must use the wake cadence, not
    # the 8s outage backoff: the network is coming up, Spotify is not down.
    retry_waits = intervals[2:4]
    assert retry_waits == [state.WAKE_RETRY_SECONDS] * 2, (
        "resume must retry at %.0fs, got %r (full sequence %r)"
        % (state.WAKE_RETRY_SECONDS, retry_waits, intervals)
    )

    # The first poll after the suspend published the same track it already
    # had. Without the force flag _should_publish suppresses that as a no-op
    # and every phone keeps waiting on a version that never moves.
    version, payload = store.snapshot()
    assert version >= 3, (
        "resume must force a publish so clients re-sync, version only %r" % version
    )
    assert payload["id"] == "a"
    assert not store._force_publish, "the force flag must be consumed"


def check_poll_failure_is_not_idle():
    """A failed poll must not be published as "nothing playing".

    now_playing() used to swallow every exception and return None, which the
    tick could not tell apart from Spotify genuinely reporting nothing. One
    DNS blip and the phone claims the music stopped.
    """
    store = state.Store(enable_lyrics=False)
    store._client = object()
    _track(store, "a")
    before, _ = store.snapshot()

    real = spotify.now_playing
    spotify.now_playing = lambda client: (_ for _ in ()).throw(
        spotify.PollFailed("getaddrinfo failed")
    )
    try:
        try:
            store._tick()
            raise AssertionError("a failed poll must raise, not return")
        except spotify.PollFailed:
            pass
    finally:
        spotify.now_playing = real

    after, payload = store.snapshot()
    assert after == before, "a failed poll must not publish anything"
    assert payload["status"] != "idle", (
        "a failed poll must not be reported as idle — we do not know what is "
        "playing, we could not ask"
    )


def check_keepalive_never_rewinds_progress():
    """Polling at the current version must never hand back a stale position.

    The long poll returns the stored payload when it times out, and that
    payload's progress_ms was frozen when it was published — up to 25 seconds
    earlier. The client re-anchors to whatever it receives, so sending it raw
    is the progress bar jumping backwards on every timeout.
    """
    _track(server.store, "a", progress=60000, playing=True, duration=300000)
    client = server.app.test_client()
    version, _ = server.store.snapshot()

    real_timeout = server.POLL_TIMEOUT
    server.POLL_TIMEOUT = 0.2          # a 25s keepalive is not a unit test
    try:
        seen = []
        for _ in range(3):
            body = client.get("/api/state?v=%d" % version).get_json()
            assert body["version"] == version, "a keepalive must not bump the version"
            seen.append(body["progress_ms"])

        for i in range(1, len(seen)):
            assert seen[i] >= seen[i - 1], (
                "progress went backwards across keepalives: %r" % seen
            )
        assert seen[-1] > 60000, (
            "progress must roll forward from the published value, got %r" % seen
        )
        assert seen[-1] < 300000, "and must never exceed the duration"

        # Paused is the other half of the contract: the position is frozen,
        # so rolling it forward would invent playback that did not happen.
        _track(server.store, "b", progress=45000, playing=False, duration=300000)
        version, _ = server.store.snapshot()
        first = client.get("/api/state?v=%d" % version).get_json()["progress_ms"]
        second = client.get("/api/state?v=%d" % version).get_json()["progress_ms"]
        assert first == second == 45000, (
            "paused progress must not advance, got %r then %r" % (first, second)
        )
    finally:
        server.POLL_TIMEOUT = real_timeout


def check_lyric_misses_expire():
    """A miss must not be permanent, and a network failure is not a miss.

    The old code wrote [] to the cache on any empty result, so a song that
    failed once was never looked up again — including after someone
    contributed the lyrics. This is the failure mode that compounds.
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    real_dir, real_lookup = lyrics.CACHE_DIR, lyrics._lookup
    lyrics.CACHE_DIR = tmp

    track = {"title": "T", "artist": "A", "album": "Al", "duration_ms": 200000}
    calls = []

    def miss(_track):
        calls.append("miss")
        return lyrics._empty(), True          # LRCLIB answered: genuinely absent

    def unreachable(_track):
        calls.append("unreachable")
        return lyrics._empty(), False         # network down: not evidence

    def hit(_track):
        calls.append("hit")
        return {"lines": [[0, "line"]], "synced": True}, True

    try:
        lyrics._lookup = miss
        lyrics.fetch(track)
        lyrics.fetch(track)
        assert calls == ["miss"], "a fresh miss must be cached, got %r" % calls

        # Age the cached miss past the TTL.
        cached = os.path.join(tmp, os.listdir(tmp)[0])
        with open(cached) as fh:
            blob = json.load(fh)
        blob["ts"] = int(time.time()) - lyrics.MISS_TTL - 60
        with open(cached, "w") as fh:
            json.dump(blob, fh)

        lyrics._lookup = hit
        result = lyrics.fetch(track)
        assert calls == ["miss", "hit"], (
            "an expired miss must be retried, got %r" % calls
        )
        assert result["lines"] == [[0, "line"]]

        # A hit is permanent — no TTL, no repeat lookups.
        lyrics.fetch(track)
        assert calls == ["miss", "hit"], "a hit must not be re-fetched: %r" % calls

        # And an unreachable LRCLIB must leave no cache behind at all.
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        lyrics._lookup = unreachable
        lyrics.fetch(track)
        lyrics.fetch(track)
        assert calls[-2:] == ["unreachable", "unreachable"], (
            "a network failure must not be cached as a miss, got %r" % calls
        )
    finally:
        lyrics.CACHE_DIR, lyrics._lookup = real_dir, real_lookup
        shutil.rmtree(tmp, ignore_errors=True)


def check_lyric_title_cleaning():
    """The suffixes Spotify adds that LRCLIB entries usually do not carry."""
    cases = [
        ("Karma Police - 2011 Remaster", "Karma Police"),
        ("Everything In Its Right Place (Remastered)", "Everything In Its Right Place"),
        ("Blue Monday - Radio Edit", "Blue Monday"),
        ("Song [Bonus Track]", "Song"),
        ("Weird Fishes / Arpeggi", "Weird Fishes / Arpeggi"),   # untouched
        ("- - -", "- - -"),                                     # never empty
    ]
    for raw, want in cases:
        got = lyrics._clean_title(raw)
        assert got == want, "clean(%r) = %r, wanted %r" % (raw, got, want)


def check_plain_lyrics_are_kept_and_flagged():
    """Plain lyrics are worth keeping; they just cannot be timed."""
    synced = lyrics._from_payload({"syncedLyrics": "[00:12.50]hello\n[00:15.00]world"})
    assert synced["synced"] is True
    assert synced["lines"] == [[12500, "hello"], [15000, "world"]]

    plain = lyrics._from_payload({"plainLyrics": "hello\n\nworld", "syncedLyrics": None})
    assert plain["synced"] is False, "plain lyrics must be flagged unsynced"
    assert plain["lines"] == [[None, "hello"], [None, "world"]], (
        "plain lyrics must be kept, with no invented timestamps: %r" % plain["lines"]
    )

    assert lyrics._from_payload({})["lines"] == []


def check_dither_is_1bit_at_display_size():
    """The e-ink cover must be a real dither, at the size it is displayed.

    Two failure modes this catches: dithering before the resize (which greys
    the pattern into mush), and a plain threshold (which gives two colours but
    no pattern at all).
    """
    import io
    import shutil
    import tempfile
    from PIL import Image

    source = Image.new("RGB", (640, 640))
    pixels = source.load()
    for y in range(640):
        for x in range(640):
            # A smooth ramp: a threshold would split it into two flat blocks,
            # error diffusion has to break it up.
            value = (x + y) // 5
            pixels[x, y] = (value, value, value)
    encoded = io.BytesIO()
    source.save(encoded, "PNG")

    class FakeResponse(object):
        content = encoded.getvalue()

    tmp = tempfile.mkdtemp()
    real_dir, real_get = art.CACHE_DIR, art.requests.get
    art.CACHE_DIR = tmp
    art.requests.get = lambda url, timeout=None: FakeResponse()
    try:
        result = art.prepare("http://example.invalid/cover.jpg")
        assert result and result.get("cover_dither"), "no dithered variant produced"
        assert result["cover_dither"].endswith(".png"), (
            "must be PNG — JPEG smears a 1-bit dither, got %r" % result["cover_dither"]
        )
        assert result.get("cover"), "the colour cover must still be produced"

        path = os.path.join(tmp, os.path.basename(result["cover_dither"]))
        image = Image.open(path)
        image.load()

        # 8-bit container, two-value content: dithered as 1-bit, then widened
        # so the decoder never has to handle a 1-bit-depth PNG.
        assert image.mode == "L", "must be 8-bit greyscale, got mode %r" % image.mode
        values = sorted(v for _, v in image.getcolors(256))
        assert values == [0, 255], (
            "widening must not introduce grey — got values %r" % values
        )
        assert image.size == (art.DITHER_PX, art.DITHER_PX), (
            "must be dithered at display size %d, got %r"
            % (art.DITHER_PX, image.size)
        )

        row = art.DITHER_PX // 2
        line = [image.getpixel((x, row)) for x in range(art.DITHER_PX)]
        flips = sum(1 for i in range(1, len(line)) if line[i] != line[i - 1])
        assert flips >= 10, (
            "middle row flips only %d times — that is a threshold, not a "
            "dither (or the pattern was resampled away)" % flips
        )

        # --- the dimmed variant ---------------------------------------------
        assert result.get("cover_dither_dim"), "no dimmed variant produced"
        dim_path = os.path.join(tmp, os.path.basename(result["cover_dither_dim"]))
        dim = Image.open(dim_path)
        dim.load()
        dim_rgb = dim.convert("RGB")

        assert dim.size == image.size, (
            "dimmed variant must match the bright one, got %r" % (dim.size,)
        )

        tones = sorted(c for _, c in dim_rgb.getcolors(256))
        assert tones == [(0, 0, 0), art.DIM_INK], (
            "dimmed variant must be exactly black and %r, got %r"
            % (art.DIM_INK, tones)
        )

        # The whole point: same photograph, same dither, only quieter. Every
        # lit pixel in the bright variant must be lit in the dim one and no
        # others — a re-dither at a different tone, or an inversion, fails here.
        bright_lit = [1 if v else 0 for v in image.getdata()]
        dim_lit = [1 if px == art.DIM_INK else 0 for px in dim_rgb.getdata()]
        assert bright_lit == dim_lit, (
            "dimmed variant is not the same pattern — %d of %d pixels differ"
            % (sum(1 for a, b in zip(bright_lit, dim_lit) if a != b), len(bright_lit))
        )
    finally:
        art.CACHE_DIR, art.requests.get = real_dir, real_get
        shutil.rmtree(tmp, ignore_errors=True)


CHECKS = [
    check_version_never_falsy,
    check_merge_does_not_rewind_progress,
    check_merge_does_not_overrun_duration,
    check_merge_drops_stale_worker,
    check_paused_merge_does_not_advance,
    check_should_publish_suppresses_noops,
    check_waiter_wakes_on_publish,
    check_future_version_does_not_block,
    check_auth_retries_until_success,
    check_resume_from_suspend,
    check_poll_failure_is_not_idle,
    check_keepalive_never_rewinds_progress,
    check_lyric_misses_expire,
    check_lyric_title_cleaning,
    check_plain_lyrics_are_kept_and_flagged,
    check_dither_is_1bit_at_display_size,
]


if __name__ == "__main__":
    failed = 0
    for check in CHECKS:
        try:
            check()
            print("  ok    %s" % check.__name__)
        except AssertionError as exc:
            failed += 1
            print("  FAIL  %s: %s" % (check.__name__, exc))
    print("\n%d/%d passed" % (len(CHECKS) - failed, len(CHECKS)))
    sys.exit(1 if failed else 0)
