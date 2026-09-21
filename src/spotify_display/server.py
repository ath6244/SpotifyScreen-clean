"""Flask server. Serves the frontend, the art cache, and the long-poll API."""

import os
import socket
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from . import art, spotify, state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(ROOT, "static")
ASSETS = os.path.join(ROOT, "static", "assets")

AUTH_RETRY_SECONDS = 5        # first auth retry; doubles up to the cap
AUTH_MAX_RETRY_SECONDS = 30
POLL_TIMEOUT = 25.0           # long-poll ceiling; the client guards at 35s


def _load_env(path):
    """Read a .env file into the environment.

    Deliberately not python-dotenv: this is a dozen lines and saves a
    dependency. Existing environment variables win, so an explicit
    `set VAR=... && python -m spotify_display.server` still overrides the file.
    """
    try:
        handle = open(path)
    except IOError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


# Must run before Store is constructed — it reads ENABLE_LYRICS at init.
_load_env(os.path.join(ROOT, ".env"))

app = Flask(__name__, static_folder=None)
store = state.Store(enable_lyrics=os.environ.get("ENABLE_LYRICS", "1") == "1")


def _nocache(resp):
    """Gingerbread's browser caches XHR responses far more eagerly than
    modern ones, and ignores query-string variation in some builds. Belt and
    braces: explicit headers here, plus a cache-buster in app.js."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return _nocache(send_from_directory(FRONTEND, "index.html"))


@app.route("/<path:filename>")
def frontend_files(filename):
    return send_from_directory(FRONTEND, filename)


@app.route("/assets/<path:filename>")
def asset_files(filename):
    return send_from_directory(ASSETS, filename)


@app.route("/art/<path:filename>")
def art_files(filename):
    # Filenames are content-hashed, so these can cache hard on the device.
    resp = send_from_directory(art.CACHE_DIR, filename)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.route("/api/state")
def api_state():
    """Long poll. Pass ?v=N to block until the state moves past version N."""
    try:
        since = int(request.args.get("v", 0))
    except ValueError:
        since = 0

    version, payload = store.snapshot()

    # since > version means the client is talking to a server that restarted
    # under it: version counters reset to 1 on boot, but the client keeps the
    # last number it saw. wait_for_change would then park it for the full 25s
    # on every single poll, and a phone whose JS timers froze with the screen
    # off never gets out of that. Answer immediately instead — the response
    # carries the real version and the client re-syncs from it.
    if 0 < since <= version:
        version, payload = store.wait_for_change(since, timeout=POLL_TIMEOUT)

    # progress_ms in the stored payload is frozen at publish time. Sent raw,
    # every timed-out long poll hands the client a position up to 25 seconds
    # stale, and a page loaded mid-track starts life behind. Roll it to send
    # time; paused tracks and the duration ceiling are handled inside.
    body = dict(state.roll_forward(payload))
    # Always stamp the version onto the response. A payload that reaches the
    # client without one reads as falsy in JS, pinning it at v=0 and turning
    # every poll into an immediate-return busy loop.
    body["version"] = version
    # Wall clock at send time, not at publish time. server_time is stamped when
    # a payload is published, so a long poll that times out returns one that may
    # be minutes old — using it to set the phone's clock would bake that lag in.
    # The phone has no NTP and its own clock is usually wrong, so this is what
    # the header clock runs on.
    body["now"] = int(time.time() * 1000)
    return _nocache(jsonify(body))


@app.route("/api/control/<action>", methods=["POST"])
def api_control(action):
    """playpause | next. The phone's only write path into Spotify."""
    try:
        store.control(action)
    except spotify.NoActiveDevice as exc:
        # Routine, not a fault: Spotify drops the active device after a while
        # idle. 409 rather than 500 so the client can tell the two apart.
        print("[control] %s" % exc)
        return _nocache(jsonify({"ok": False, "error": "no_active_device"})), 409
    except ValueError:
        return _nocache(jsonify({"ok": False, "error": "unknown_action"})), 404
    except Exception as exc:
        # Never let a control failure take the process down — the poller thread
        # is what keeps the display alive, and it is unaffected by this.
        print("[control] %s failed: %s" % (action, exc))
        return _nocache(jsonify({"ok": False, "error": "failed"})), 502

    return _nocache(jsonify({"ok": True}))


@app.route("/healthz")
def healthz():
    version, payload = store.snapshot()
    return jsonify({"version": version, "status": payload.get("status")})


def _ip_toward(host):
    """Local address the OS would use to reach `host`. Sends nothing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 80))
        return sock.getsockname()[0]
    except Exception:
        return None
    finally:
        sock.close()


def _authenticate():
    """store.authenticate() — which is spotify.make_client() — retried forever.

    A laptop cold-booting frequently has no DNS until Wi-Fi association
    finishes, which can take well past 25 seconds. The token refresh then
    fails on name resolution alone.

    This never gives up on purpose. The server is launched from a startup
    .bat on login, so exiting means a dead display and nobody to restart it.
    A display that comes up two minutes late beats one that never comes up.
    """
    delay = AUTH_RETRY_SECONDS
    attempt = 0
    while True:
        try:
            store.authenticate()
            return
        except Exception as exc:
            attempt += 1
            print("")
            print("  Spotify auth attempt %d failed: %s" % (attempt, exc))
            print("  No network yet, or Spotify is unreachable. Check the")
            print("  laptop's Wi-Fi. Retrying in %ds." % delay)
            time.sleep(delay)
            delay = min(delay * 2, AUTH_MAX_RETRY_SECONDS)


def main():
    port = int(os.environ.get("PORT", "8080"))

    # Authenticate before the port opens. If OAuth is still pending there is
    # nothing worth serving, and a phone polling a half-started server is
    # exactly the case that used to spin.
    _authenticate()

    poller = threading.Thread(target=store.run_forever, name="spotify-poller")
    poller.daemon = True
    poller.start()

    # Wi-Fi, not USB tethering: the phone reaches the laptop over the LAN, so
    # the default-route address is the one to type into its browser.
    lan = _ip_toward("8.8.8.8")
    print("")
    if lan:
        print("  Open this on the phone:  http://%s:%d/" % (lan, port))
        print("  Phone and laptop must be on the same Wi-Fi network.")
    else:
        print("  Could not determine this laptop's LAN address.")
        print("  Check its Wi-Fi connection, then restart.")
    print("")

    # threaded=True is required: every long poll parks a worker for up to 25s.
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
