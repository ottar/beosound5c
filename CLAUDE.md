# BeoSound 5c

Custom firmware/UI turning a Raspberry Pi into the brain of a Bang & Olufsen
BeoSound 5: the physical wheel, laser pointer and GO/‹/› buttons drive a
kiosk web UI plus a set of Python microservices.

## Project focus — Music Assistant only

**We only care about Music Assistant as the audio source, and about adapting
the UI to it.** Do NOT invest effort in Sonos, Spotify, Apple Music, TIDAL,
Plex, BlueSound or any other source/player integration — code for those may
still exist in the tree, but it is not a target. When making changes, optimise
for the `music_assistant` player + source and the BeoSound 5 look/feel; don't
add features, tests, or polish for the other backends, and don't let them
constrain MA-focused designs. The live device runs `player.type=music_assistant`
with the `music_assistant` volume adapter.

## Layout

- `services/` — Python microservices, one per unit (`beo-*.service`), each on a
  fixed port. Key ones for MA:
  - `players/music_assistant_player.py` (beo-player-music-assistant, :8766) —
    playback, speaker grouping, target selection, group-aware volume.
  - `sources/music_assistant/service.py` (beo-source-music-assistant, :8780) —
    library + Discover browse.
  - `router.py` (beo-router, :8770) — event routing, volume adapter, menu.
  - `input.py` (beo-input, :8765) — HID wheel/laser/buttons → WebSocket.
  - `lib/ma_client.py` — shared MA websocket client.
- `web/` — kiosk UI (served by beo-http on **port 80**). `js/` shared, `softarc/`
  arc-list views (`script-v2.js` is the ArcList), `sources/<id>/view.js` per
  source. `js/speaker-overlay.js` = the double-GO speaker overlay.
- `config/default.json` — repo template. Live config is
  `/etc/beosound5c/config.json`; secrets in `/etc/beosound5c/secrets.env`
  (root-only; MASS_TOKEN/MASS_WS_URL for MA).
- `tests/unit/python/` — pytest. Run `python3 -m pytest tests/unit/python -q`.

## Working on the device

The repo is checked out at `/home/ottar/bs5c`; `/home/ottar/beosound5c` is a
symlink to it, and services are served/run from there — so edits are live
immediately. To apply:

- Frontend (web/): `sudo systemctl restart beo-ui` (kiosk reloads).
- A service (services/): `sudo systemctl restart beo-<name>` — e.g.
  `beo-router`, `beo-player-music-assistant`, `beo-source-music-assistant`,
  `beo-input`.
- Config/player-type change: also run
  `sudo bash services/system/reconcile-services.sh` (swaps active player unit).

`ottar` has passwordless sudo for `systemctl start/stop/restart beo-*`,
`tee/cat /etc/beosound5c/{config.json,secrets.env}`, reconcile and post-update;
general sudo needs a password.

## Hardware / UI notes

- Display is exactly **1024×768** (HDMI-1). The volume wheel arc is centred at
  (1024,384) r274 → its left edge sits at x≈750; arc lists hug just left of it.
- Input model (`input.py`): nav wheel (`nav`) scrolls lists; volume wheel
  (`volume`) = volume; laser pointer (`laser`) picks the left main menu; GO/
  ‹/› buttons. GO is edge-detected (short `go` / long `go_long`).
- Verify UI changes by deploying and looking on the device — do not spend time
  on headless-chromium screenshot renders.
- Never trigger real speaker playback in tests; use fakes/the emulator. For
  live B&O debugging, ask the user for physical confirmation.
