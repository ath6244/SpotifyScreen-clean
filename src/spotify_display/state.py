"""Single source of truth for what the phone should be showing.

One background thread polls Spotify. Clients block on a condition variable
until the version number changes. This is the whole real-time mechanism —
there is no WebSocket, because the target browser has no WebSocket.

Publishing a track change is staged in three parts: metadata immediately,
then art, then lyrics. Nothing slow is allowed to sit between the user
pressing skip and the phone showing the new title.
"""

import threading
import time

from . import art, lyrics, spotify

POLL_SECONDS = 3.0        # Spotify's practical floor for this endpoint
IDLE_POLL_SECONDS = 8.0   # back off when nothing is playing
SEEK_TOLERANCE_MS = 1500  # drift past this reads as a seek, not clock skew

# The laptop is allowed to sleep, including overnight. A wait of 3 seconds
# that took an hour of wall clock means the machine was suspended, not that
# anything is wrong, and the polls immediately after it will fail while the
# Wi-Fi re-associates and DNS comes back. That is a resume, and it gets a
# tighter retry than a genuine outage.
WAKE_GAP_SECONDS = 60      # slept this much longer than asked = suspended
WAKE_RETRY_SECONDS = 2.0   # retry cadence while recovering
WAKE_RECOVERY_SECONDS = 60  # how long to keep trying before normal backoff


def roll_forward(payload, now_ms=None):
    """Return a copy of `payload` with progress_ms advanced to now.

    A stored payload freezes progress_ms at the moment it was published. Any
    consumer that treats that number as current — a long poll returning a
    payload published 25 seconds ago, an enrichment merge, a browser loading
    the page mid-track — reads a position that far in the past, and the
    client re-anchors its progress clock to it. On screen that is the bar
    jumping backwards.

    Paused playback does not advance, and nothing runs past the duration.
    """
    if not payload.get("is_playing"):
        return payload

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    elapsed = max(now_ms - payload.get("server_time", now_ms), 0)
    advanced = payload.get("progress_ms", 0) + elapsed
    duration = payload.get("duration_ms") or advanced

    out = dict(payload)
    out["progress_ms"] = min(advanced, duration)
    return out


class Store(object):
    def __init__(self, enable_lyrics=True):
        self._lock = threading.Condition()
        # Version starts at 1, not 0. The client tests `if (payload.version)`
        # and 0 is falsy in JS, which would pin it at v=0 forever — every
        # poll would take the immediate-return branch and spin.
        self._version = 1
        self._payload = {
            "status": "starting",
            "version": 1,
            "server_time": int(time.time() * 1000),
        }
        self._enable_lyrics = enable_lyrics
        self._client = None
        self._last_track_id = None
        # Lets a control command cut the poll interval short instead of
        # waiting up to 3 seconds for the loop to come round again. All
        # polling still happens on the one poller thread.
        self._wake = threading.Event()
        # Set when the next successful poll must publish regardless of whether
        # anything changed. Used after a resume from suspend.
        self._force_publish = False

    # ---- reading -------------------------------------------------------

    def snapshot(self):
        with self._lock:
            return self._version, self._payload

    def wait_for_change(self, since_version, timeout=25.0):
        """Block until version > since_version, or timeout. Returns a snapshot.

        On timeout we return the current state anyway. The client treats that
        as a keepalive and immediately re-polls, which also keeps Android's
        aggressive connection teardown from looking like an error.
        """
        deadline = time.time() + timeout
        with self._lock:
            while self._version <= since_version:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
            return self._version, self._payload

    # ---- writing -------------------------------------------------------

    def _publish_locked(self, payload):
        """Stamp and hand over a new payload. Caller must hold the lock.

        Payloads are replaced wholesale rather than mutated, so a reader
        holding an older reference keeps a consistent view of it.
        """
        self._version += 1
        payload["version"] = self._version
        payload["server_time"] = int(time.time() * 1000)
        self._payload = payload
        self._lock.notify_all()

    def _publish(self, payload):
        with self._lock:
            self._publish_locked(payload)

    def _merge(self, track_id, extra):
        """Publish an addition to the track already on screen.

        Two things matter here. It must no-op if the track moved on while we
        were fetching — skipping quickly leaves stale workers in flight. And
        it must roll progress_ms forward: the client re-anchors its progress
        clock on every payload it receives, so republishing a progress_ms
        captured four seconds ago would make the bar visibly jump backwards.
        """
        with self._lock:
            if self._payload.get("id") != track_id:
                return

            payload = roll_forward(self._payload)
            payload = dict(payload)
            payload.update(extra)

            self._publish_locked(payload)

    # ---- lifecycle -----------------------------------------------------

    def authenticate(self):
        """Run the OAuth flow.

        Called from main() before the listening port opens, so the phone can
        never reach a server that is still waiting on consent.
        """
        self._client = spotify.make_client()
        print("[state] authenticated")

    def control(self, action):
        """Run a transport command against the active Spotify device.

        Raises spotify.NoActiveDevice when Spotify has forgotten the device,
        which is routine rather than a fault. Runs on the request thread; the
        only thing it touches is the wake event, so the poller keeps its
        monopoly on publishing.
        """
        if self._client is None:
            raise RuntimeError("authenticate() must be called first")

        if action == "playpause":
            _, payload = self.snapshot()
            if payload.get("is_playing"):
                spotify.pause(self._client)
            else:
                spotify.play(self._client)
        elif action == "next":
            spotify.skip(self._client)
        else:
            raise ValueError("unknown action %r" % action)

        # Spotify needs a moment before /me/player reports the new state.
        # Without this the immediate tick reads the old one, _should_publish
        # suppresses it as a no-op, and the phone waits out the full interval.
        time.sleep(0.35)
        self._wake.set()

    def _sleep(self, seconds):
        """Wait, and report how much wall clock actually passed.

        A control command sets the event and returns early, which reads as a
        shorter sleep. A suspend reads as a much longer one — that is the
        signal this exists for.
        """
        started = time.time()
        self._wake.wait(seconds)
        self._wake.clear()
        return time.time() - started

    def run_forever(self):
        if self._client is None:
            raise RuntimeError("authenticate() must be called before run_forever()")
        print("[state] polling every %.0fs" % POLL_SECONDS)

        interval = POLL_SECONDS
        recovering_until = 0.0

        while True:
            slept = self._sleep(interval)

            if slept > interval + WAKE_GAP_SECONDS:
                # Wall clock jumped: the machine was suspended. Nothing is
                # broken, but the network is not up yet.
                print("[state] resumed after %.0fs suspended" % slept)
                recovering_until = time.time() + WAKE_RECOVERY_SECONDS
                # Every connected phone has been staring at a frozen payload.
                # Force one publish on the first poll that works so they all
                # get a new version and re-sync, even if the track is the same.
                self._force_publish = True

            try:
                interval = self._tick()
                recovering_until = 0.0
            except spotify.PollFailed as exc:
                if time.time() < recovering_until:
                    # Expected during a resume. Retry hard rather than backing
                    # off as though Spotify were down.
                    print("[state] waking, network not ready: %s" % exc)
                    interval = WAKE_RETRY_SECONDS
                else:
                    print("[state] poll failed: %s" % exc)
                    interval = IDLE_POLL_SECONDS
            except Exception as exc:
                print("[state] tick failed: %s" % exc)
                interval = IDLE_POLL_SECONDS

    # ---- polling -------------------------------------------------------

    def _tick(self):
        # Raises PollFailed if the poll itself did not work; None here means
        # the poll worked and nothing is playing.
        track = spotify.now_playing(self._client)
        _, previous = self.snapshot()

        # Read only after a successful poll, so a failing one leaves the flag
        # set for the next attempt.
        force = self._force_publish
        self._force_publish = False

        if track is None:
            if force or previous.get("status") != "idle":
                self._last_track_id = None
                self._publish({"status": "idle"})
            return IDLE_POLL_SECONDS

        payload = {
            "status": "playing" if track["is_playing"] else "paused",
            "id": track["id"],
            "title": track["title"],
            "artist": track["artist"],
            "album": track["album"],
            "duration_ms": track["duration_ms"],
            "progress_ms": track["progress_ms"],
            "is_playing": track["is_playing"],
        }

        if track["id"] != self._last_track_id:
            self._last_track_id = track["id"]
            # Phase one: metadata only, unconditional, right now. Art and
            # lyrics follow on their own thread.
            payload["art"] = None
            payload["lyrics"] = []
            payload["lyrics_synced"] = False
            self._publish(payload)
            self._start_enrichment(track)
        else:
            payload["art"] = previous.get("art")
            payload["lyrics"] = previous.get("lyrics", [])
            payload["lyrics_synced"] = previous.get("lyrics_synced", False)
            # force wins over the no-op suppressor: after a resume the phones
            # need a version bump even when the same track is still playing.
            if force or self._should_publish(payload):
                self._publish(payload)

        return POLL_SECONDS

    def _should_publish(self, payload):
        """Suppress no-op updates.

        The phone interpolates progress locally, so a new frame is only worth
        sending when something it cannot predict has changed: the track, the
        play/pause state, or a seek.
        """
        with self._lock:
            prev = self._payload
            if prev.get("id") != payload["id"]:
                return True
            if prev.get("is_playing") != payload["is_playing"]:
                return True

            elapsed = (time.time() * 1000) - prev.get("server_time", 0)
            expected = prev.get("progress_ms", 0) + (elapsed if prev.get("is_playing") else 0)
            return abs(payload["progress_ms"] - expected) > SEEK_TOLERANCE_MS

    # ---- enrichment ----------------------------------------------------

    def _start_enrichment(self, track):
        worker = threading.Thread(
            target=self._enrich, args=(track,), name="enrich"
        )
        worker.daemon = True
        worker.start()

    def _enrich(self, track):
        """Fetch art, then lyrics, publishing after each.

        Off the poll thread entirely: a slow LRCLIB lookup must not delay
        detecting that the track changed again. _merge drops the result if
        it did.
        """
        track_id = track["id"]

        try:
            self._merge(track_id, {"art": art.prepare(track["art_url"])})
        except Exception as exc:
            print("[state] art failed: %s" % exc)

        if not self._enable_lyrics:
            return

        try:
            found = lyrics.fetch(track)
            self._merge(track_id, {
                "lyrics": found["lines"],
                # Plain lyrics carry no timestamps, so the client cannot walk
                # them against progress_ms. The flag says which it is holding.
                "lyrics_synced": found["synced"],
            })
        except Exception as exc:
            print("[state] lyrics failed: %s" % exc)
