# Architecture

The laptop runs the Python server and owns Spotify authentication. The Android 2.3 phone only loads the HTTP display over local Wi-Fi.

- `src/spotify_display`: Flask server, Spotify integration, state polling, album-art processing, and lyrics lookup.
- `static`: HTML, JavaScript, CSS, and display themes served to the phone.
- `config/.env.example`: configuration template. The real `.env` stays local.
- `var/cache`: generated album art and lyrics cache.
- `var/logs`: launcher output.
- `tests`: offline behavior checks.
- `scripts`: operator utilities.
