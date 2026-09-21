/* Written for the Android 2.3 stock browser (WebKit 533).
   No const/let, no arrow functions, no forEach, no rAF, no WebSocket,
   no EventSource, no XHR2 (so no xhr.timeout, no onload). JSON is native
   in 2.2+, so JSON.parse is safe. */

(function () {
  "use strict";

  var POLL_GUARD_MS = 35000;   // client-side abort; server times out at 25s
  var TICK_MS = 500;           // progress redraw interval
  var RETRY_MS = 3000;         // first retry; doubles per consecutive failure
  var MAX_RETRY_MS = 30000;    // ceiling for the backoff
  var RESYNC_AFTER = 3;        // failures before dropping back to v=0
  var DEAD_AFTER = 5;          // failures before the screen goes to clock-only
  var URL_BAR_MS = 200;        // delay before re-scrolling and measuring

  // Waveform geometry. Must match #waveform's width in the theme that shows
  // it: BARS * PITCH is the container width, and MAX is its height.
  var WAVE_BARS = 32;
  // Pitch 14 (10px bar + 4px gap), not 8: at a metre, thirty-odd thin bars
  // read as noise rather than as a shape. Must match .wave-bar's width.
  var WAVE_PITCH = 14;
  var WAVE_MIN = 4;
  var WAVE_MAX = 30;

  var el = {};
  var version = 0;
  var state = null;
  var anchorProgress = 0;      // progress_ms at the moment of the last update
  var anchorLocal = 0;         // local clock at that same moment
  var lyricIndex = -1;
  var currentCover = "";
  var currentDither = "";
  var currentDitherDim = "";
  var failures = 0;
  var fitPending = false;
  var clockOffset = 0;         // server wall clock minus this device's
  var waveBars = null;
  var waveLit = -1;            // index of the last bar drawn as played
  var lastTrackId = "";        // kept so the waveform can be rebuilt on rotate

  // <body> carries two independent classes: what the player is doing
  // (is-idle / is-dead) and which way up the phone is. They are tracked
  // separately because className is written wholesale — the old code set
  // "is-idle" directly, which would wipe the orientation with it.
  var stateClass = "";
  var orientClass = "";
  var debugClass = "";         // "is-debug" when ?debug=1, set once at init

  function $(id) { return document.getElementById(id); }

  function init() {
    el.body = document.body;
    el.backdrop = $("backdrop");
    el.cover = $("cover");
    el.title = $("title");
    el.artist = $("artist");
    el.lyric = $("lyric");
    el.fill = $("fill");
    el.elapsed = $("elapsed");
    el.duration = $("duration");
    el.badge = $("badge");
    el.clock = $("clock");
    el.offline = $("offline");
    el.screen = $("screen");
    el.coverDither = $("cover-dither");
    el.coverDitherDim = $("cover-dither-dim");
    el.albumName = $("album-name");
    el.statusTime = $("status-time");
    el.now = $("now-label");
    el.wave = $("waveform");
    el.btnPlay = $("btn-play");
    el.btnNext = $("btn-next");

    if (window.location.search.indexOf("debug=1") >= 0) { debugClass = "is-debug"; }

    applyTheme();
    bindControls();
    hideUrlBar();
    poll();
    setInterval(tick, TICK_MS);

    // No fullscreen API on Gingerbread — requestFullscreen does not exist and
    // the vendor-prefixed forms silently no-op. The scroll trick is the only
    // way to get the URL bar off the screen.
    if (window.addEventListener) {
      window.addEventListener("orientationchange", hideUrlBar, false);
      window.addEventListener("resize", hideUrlBar, false);
    } else {
      window.onorientationchange = hideUrlBar;
      window.onresize = hideUrlBar;
    }
  }

  /* --- viewport -------------------------------------------------------
     Scrolling one pixel down retracts the URL bar. body is taller than the
     viewport (style.css) so the scroll has somewhere to go. */

  function hideUrlBar() {
    // Retracting the bar fires resize, which lands back here. Without the
    // latch that is an endless chain of timers.
    if (fitPending) { return; }
    fitPending = true;

    window.scrollTo(0, 1);
    // The first call lands before layout settles on WebKit 533, so innerHeight
    // read now is still the pre-retract value. Repeat, then measure.
    setTimeout(function () {
      window.scrollTo(0, 1);
      fitScreen();
      fitPending = false;
    }, URL_BAR_MS);
  }

  function fitScreen() {
    var h = window.innerHeight;
    if (h > 0) { el.screen.style.height = h + "px"; }
    setOrientation();

    // /?theme=eink&debug=1 prints the measured viewport where the header label
    // goes. There is no console on this browser and no remote debugger for
    // WebKit 533, so this is the only way to read the numbers off the device.
    if (window.location.search.indexOf("debug=1") >= 0) {
      setText(el.now, "W " + window.innerWidth + " H " + h);
    }
  }

  /* --- orientation -----------------------------------------------------
     Measured, not assumed. Landscape is the intended orientation, but a
     bumped phone or a misfiring auto-rotate has to land on a layout that
     works rather than on one drawn for the other aspect ratio. */

  function setOrientation() {
    var next = window.innerWidth > window.innerHeight ? "is-landscape" : "is-portrait";
    if (next === orientClass) { return; }
    orientClass = next;
    applyBodyClass();
    // The waveform's bar count comes from the container width, which just
    // changed. Same track id, so the pattern is the same fingerprint.
    buildWaveform(lastTrackId);
  }

  function setStateClass(cls) {
    stateClass = cls;
    applyBodyClass();
  }

  function applyBodyClass() {
    var cls = stateClass ? (stateClass + " " + orientClass) : orientClass;
    el.body.className = debugClass ? (cls + " " + debugClass) : cls;
  }

  /* --- controls -------------------------------------------------------
     The only write path from the phone into Spotify. Two buttons, because a
     previous button needs a second endpoint and a rule about what "previous"
     means mid-track.

     Feedback is optimistic: the glyph flips on tap and the poll confirms a
     moment later. Waiting for the round trip before redrawing reads as a
     dead button on a device this slow. */

  function bindControls() {
    if (!el.btnPlay) { return; }
    listen(el.btnPlay, "click", function () { togglePlay(); });
    listen(el.btnNext, "click", function () { sendControl("next"); });

    // Gingerbread will not apply :active to an element with no touch
    // listener, so these empty handlers are load-bearing — they are what
    // makes the press state visible.
    listen(el.btnPlay, "touchstart", function () {});
    listen(el.btnNext, "touchstart", function () {});
  }

  function listen(node, event, fn) {
    if (node.addEventListener) {
      node.addEventListener(event, fn, false);
    } else {
      node.attachEvent("on" + event, fn);
    }
  }

  function togglePlay() {
    if (!state) { return; }
    var wanted = !state.is_playing;
    setPlaying(wanted);
    sendControl("playpause", function () { setPlaying(!wanted); });
  }

  /* Re-anchor as well as flip: tick() interpolates from anchorProgress and
     anchorLocal, so pausing without moving the anchor to the position
     currently on screen makes the bar jump when it resumes. */
  function setPlaying(on) {
    if (!state) { return; }
    anchorProgress = interpolated();
    anchorLocal = (new Date()).getTime();
    state.is_playing = on;
    setText(el.badge, on ? "Playing" : "Paused");
    paintPlayButton(on);
    tick();
  }

  function paintPlayButton(on) {
    if (el.btnPlay) { el.btnPlay.className = on ? "btn playing" : "btn"; }
  }

  function sendControl(action, onFail) {
    var xhr = new XMLHttpRequest();
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) { return; }
      if (xhr.status === 200) { return; }
      // 409 is Spotify having forgotten the active device; anything else is a
      // real failure. Both look the same from here: the command did not run.
      if (onFail) { onFail(); }
      flashOffline();
    };
    xhr.open("POST", "/api/control/" + action, true);
    xhr.send(null);
  }

  function flashOffline() {
    el.offline.style.display = "block";
    setTimeout(function () { showOffline(true); }, 2500);
  }

  /* --- theming --------------------------------------------------------
     Theme is chosen entirely client-side: /?theme=cyberpunk. The backend
     has no idea themes exist. */

  function applyTheme() {
    var match = window.location.search.match(/[?&]theme=([a-z0-9_-]+)/i);
    if (match) {
      $("theme").href = "themes/" + match[1] + ".css";
    }
  }

  /* --- transport ------------------------------------------------------
     Long poll, not WebSocket. The stock Gingerbread browser has no
     WebSocket constructor at all. */

  function poll() {
    var xhr = new XMLHttpRequest();
    var settled = false;

    // Cache-buster: some Gingerbread builds serve XHR responses from cache
    // even with no-store, if only the path is compared.
    var url = "/api/state?v=" + version + "&_=" + (new Date()).getTime();

    var guard = setTimeout(function () {
      if (settled) { return; }
      settled = true;
      try { xhr.abort(); } catch (e) {}
      // Counts as a failure, and this is the case that matters most: when the
      // laptop suspends mid-poll the socket is never refused, it simply goes
      // silent. Re-polling without counting it meant the phone looped here
      // every 35 seconds all night, never reaching the failure count that
      // turns the screen dark, sitting on a stale track instead.
      fail();
    }, POLL_GUARD_MS);

    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4 || settled) { return; }
      settled = true;
      clearTimeout(guard);

      if (xhr.status !== 200) {
        fail();
        return;
      }

      var payload;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch (e) {
        fail();
        return;
      }

      failures = 0;
      showOffline(false);

      // Same version means the long poll timed out with nothing new: a
      // keepalive, not an update. Re-rendering it would re-anchor the
      // progress clock to a payload published up to 25 seconds ago, which
      // is the bar visibly jumping backwards every time the poll expires.
      var keepalive = payload.version && payload.version === version;

      if (payload.version) { version = payload.version; }
      // Stamped at send time by the server. The phone's own clock is usually
      // wrong and there is no NTP on Gingerbread, so every displayed time is
      // the laptop's, shifted by this offset.
      if (payload.now) { clockOffset = payload.now - (new Date()).getTime(); }
      if (!keepalive) { render(payload); }
      schedule(0);
    };

    xhr.open("GET", url, true);
    xhr.send(null);
  }

  function schedule(delay) {
    setTimeout(poll, delay || 0);
  }

  function fail() {
    failures++;
    showOffline(true);

    // The server's version counter restarts at 1, but this client keeps the
    // last number it saw across the restart. Asking for a version the server
    // never issued is how a client strands itself, so once the failures look
    // persistent rather than momentary, re-sync from scratch: v=0 always
    // returns the current state immediately.
    if (failures >= RESYNC_AFTER) { version = 0; }

    // Past this many failures the laptop is off, not briefly busy. Showing
    // the track it was playing an hour ago is a lie the display keeps telling
    // until someone touches it, so fall back to the clock: black, deliberate,
    // and obviously not playing anything. render() clears this on the first
    // payload that arrives.
    if (failures >= DEAD_AFTER) {
      state = null;
      setStateClass("is-idle is-dead");
      updateClock();
    }

    // Backoff: 3s, 6s, 12s, 24s, then 30s forever. A dead server polled every
    // 3s all night is the phone's battery gone by morning.
    var delay = RETRY_MS * Math.pow(2, failures - 1);
    schedule(delay > MAX_RETRY_MS ? MAX_RETRY_MS : delay);
  }

  function showOffline(on) {
    el.offline.style.display = (on && failures > 1) ? "block" : "none";
  }

  /* --- rendering ------------------------------------------------------
     Every DOM write is guarded by a comparison. Reflow on this device costs
     tens of milliseconds; a blind rewrite every 3 seconds is visible. */

  function render(p) {
    if (p.status === "idle" || p.status === "starting") {
      state = null;
      setStateClass("is-idle");
      updateClock();
      return;
    }

    setStateClass("");

    var isNewTrack = !state || state.id !== p.id;
    state = p;

    anchorProgress = p.progress_ms;
    anchorLocal = (new Date()).getTime();

    if (isNewTrack) {
      lyricIndex = -1;
      setText(el.title, p.title);
      setText(el.artist, p.artist);
      // Spotify sets album == track name on singles, which renders as the
      // title twice. On a real album it is worth showing.
      setText(el.albumName, same(p.album, p.title) ? "" : p.album);
      setText(el.duration, fmt(p.duration_ms));
      el.lyric.innerHTML = "";
      buildWaveform(p.id);
    }

    // Outside the isNewTrack branch on purpose: art arrives in a later
    // payload than the metadata it belongs to. applyArt() no-ops when the
    // cover has not changed, and leaves the old one up while art is null.
    applyArt(p.art);

    setText(el.badge, p.is_playing ? "Playing" : "Paused");
    // The server is the authority: this corrects an optimistic flip that the
    // command turned out not to achieve.
    paintPlayButton(p.is_playing);
    tick();
  }

  function applyArt(art) {
    // Null means the art for this track is still being fetched. Keep
    // whatever is on screen; blanking it flashes an empty box on every skip.
    if (!art) { return; }

    if (art.cover !== currentCover) {
      currentCover = art.cover;
      swapImage(el.cover, art.cover);
    }

    // Every variant is always populated; the active theme decides which one is
    // visible. app.js has no idea which theme is loaded, and must not.
    if (art.cover_dither && art.cover_dither !== currentDither) {
      currentDither = art.cover_dither;
      swapImage(el.coverDither, art.cover_dither);
    }

    if (art.cover_dither_dim && art.cover_dither_dim !== currentDitherDim) {
      currentDitherDim = art.cover_dither_dim;
      swapImage(el.coverDitherDim, art.cover_dither_dim);
    }

    if (art.backdrop) {
      el.backdrop.style.backgroundImage = "url(" + art.backdrop + ")";
    }
    if (art.accent) {
      // Themes with a fixed palette override this with !important.
      el.fill.style.backgroundColor = art.accent;
      el.badge.style.color = art.accent;
    }
  }

  function swapImage(node, url) {
    // Preload so the old cover stays up until the new one is decodable.
    // Gingerbread paints a white box during an <img> src swap otherwise.
    var pre = new Image();
    pre.onload = function () { node.src = url; };
    pre.src = url;
  }

  function setText(node, value) {
    var next = value || "";
    if (node.getAttribute("data-v") === next) { return; }
    node.setAttribute("data-v", next);
    node.innerHTML = escapeHtml(next);
  }

  function same(a, b) {
    // No String.trim(): it is ES5 and this engine predates it in places.
    return (a || "").replace(/^\s+|\s+$/g, "").toLowerCase() ===
           (b || "").replace(/^\s+|\s+$/g, "").toLowerCase();
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* --- local interpolation --------------------------------------------
     The server deliberately does not push a frame every second. The phone
     runs the clock itself and gets corrected on the next real update. */

  function tick() {
    if (!state) {
      updateClock();
      return;
    }

    var ms = interpolated();
    var pct = state.duration_ms ? (ms / state.duration_ms) : 0;
    // Percentage rather than pixels: a theme can resize #track without
    // app.js needing to know the new width.
    el.fill.style.width = (pct * 100).toFixed(1) + "%";
    setText(el.elapsed, fmt(ms));

    updateLyric(ms);
    updateWaveform(pct);
    updateClock();
  }

  function interpolated() {
    if (!state) { return 0; }
    var ms = anchorProgress;
    if (state.is_playing) {
      ms += (new Date()).getTime() - anchorLocal;
    }
    return ms > state.duration_ms ? state.duration_ms : ms;
  }

  function updateLyric(ms) {
    var lines = state.lyrics;
    if (!lines || !lines.length) { return; }

    // Unsynced lyrics have no timestamps to walk — LRCLIB only had a plain
    // text version. They travel in the payload so this can change its mind
    // later, but there is nothing honest to show on a timed line.
    if (state.lyrics_synced === false) { return; }

    var i = lyricIndex;
    // Walk forward; only rewind on a seek. Cheaper than a search every tick.
    if (i >= 0 && lines[i][0] > ms) { i = -1; }
    while (i + 1 < lines.length && lines[i + 1][0] <= ms) { i++; }

    if (i !== lyricIndex) {
      lyricIndex = i;
      el.lyric.innerHTML = i >= 0 ? escapeHtml(lines[i][1]) : "";
    }
  }

  function updateClock() {
    var d = new Date((new Date()).getTime() + clockOffset);
    var h = d.getHours();
    var m = d.getMinutes();
    var text = (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
    setText(el.clock, text);
    setText(el.statusTime, text);
  }

  /* --- waveform --------------------------------------------------------
     Decorative, and honest about it. There is no audio data to draw from:
     Spotify withdrew the audio-analysis endpoints in November 2024. So the
     bars are a fingerprint of the track id — stable for a given song, but
     unrelated to its sound — and only the scan position moves. Bars that
     wiggled in time to nothing would read as a broken visualiser; a fixed
     pattern with a play position reads as designed.

     Redrawn from tick(), so 2fps. Only the bars that changed state are
     touched; repainting all 32 twice a second is visible on this device. */

  function buildWaveform(id) {
    var seed = hash(id || "");
    var html = "";
    var i, h;

    // Bar count and height come from the container, which is a different size
    // in each orientation. offsetWidth is 0 when the theme hides the waveform
    // entirely, hence the fallbacks.
    var width = el.wave.offsetWidth || (WAVE_BARS * WAVE_PITCH);
    var tall = el.wave.offsetHeight || WAVE_MAX;
    var bars = Math.floor(width / WAVE_PITCH);
    if (bars < 8) { bars = 8; }

    lastTrackId = id || "";

    for (i = 0; i < bars; i++) {
      // Linear congruential step: same id, same bars, every time — including
      // across reloads and across restarts of the server.
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      h = WAVE_MIN + (seed % (tall - WAVE_MIN + 1));
      // No whitespace between the divs: the collection below must be bars only.
      html += '<div class="wave-bar" style="left:' + (i * WAVE_PITCH) +
              "px;top:" + (tall - h) + "px;height:" + h + 'px"></div>';
    }

    el.wave.innerHTML = html;
    waveBars = el.wave.getElementsByTagName("div");
    waveLit = -1;
  }

  function updateWaveform(pct) {
    if (!waveBars || !waveBars.length) { return; }

    var idx = Math.floor(pct * waveBars.length) - 1;
    if (idx > waveBars.length - 1) { idx = waveBars.length - 1; }
    if (idx < -1) { idx = -1; }
    if (idx === waveLit) { return; }

    var i;
    if (idx > waveLit) {
      for (i = waveLit + 1; i <= idx; i++) { waveBars[i].className = "wave-bar on"; }
    } else {
      for (i = waveLit; i > idx; i--) { waveBars[i].className = "wave-bar"; }
    }
    waveLit = idx;
  }

  function hash(s) {
    var h = 5381;
    var i;
    for (i = 0; i < s.length; i++) {
      h = ((h * 33) ^ s.charCodeAt(i)) & 0x7fffffff;
    }
    return h || 1;
  }

  function fmt(ms) {
    var total = Math.floor((ms || 0) / 1000);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  if (document.addEventListener) {
    document.addEventListener("DOMContentLoaded", init, false);
  } else {
    window.onload = init;
  }
})();
