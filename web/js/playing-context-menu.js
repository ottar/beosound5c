// Playing Context Menu
//
// Hold-GO action menu for the PLAYING view / now-playing screensaver.
// The MA arc lists get their context menu inside the iframe (script-v2.js);
// PLAYING is rendered by the main page, so it gets this native overlay
// instead — same interaction model: hold GO to open, nav wheel to choose,
// release GO to run the highlighted action (Cancel is highlighted first).
//
// Actions act on the current playback via the player service:
//   SHUFFLE ON/OFF — POST /player/shuffle (state from /player/status)
//   TRACK RADIO    — POST /player/play_track_radio seeded by the playing track
//
// Visually a sibling of the speaker overlay (js/speaker-overlay.js): same
// right-side placement clear of the volume wheel, names-only rows, idle
// timeout, laser-navigation dismiss.

const PlayingContextMenu = (() => {
    const PLAYER_URL = () => (window.AppConfig?.playerUrl) || 'http://localhost:8766';
    const TIMEOUT_MS = 15000;

    let isOpen = false;
    let actions = [];       // [{id, name, run}]
    let selectedIndex = 0;
    let idleTimer = null;
    let el = null;
    let lastStepAt = 0;
    const STEP_MIN_MS = 220; // same nav-burst throttle as the speaker overlay

    function ensureEl() {
        if (el) return el;
        el = document.createElement('div');
        el.id = 'playing-context-menu';
        el.innerHTML = '<div class="pcm-title">OPTIONS</div><div class="pcm-list"></div>' +
            '<div class="pcm-help">WHEEL — CHOOSE<br>RELEASE GO — SELECT</div>';
        document.body.appendChild(el);
        return el;
    }

    async function buildActions() {
        const base = PLAYER_URL();
        let status = {};
        let trackUri = '';
        try {
            const [statusResp, uriResp] = await Promise.all([
                fetch(`${base}/player/status`),
                fetch(`${base}/player/track_uri`),
            ]);
            if (statusResp.ok) status = await statusResp.json();
            if (uriResp.ok) trackUri = (await uriResp.json()).track_uri || '';
        } catch (e) {
            console.warn('[PlayingContextMenu] status fetch failed:', e);
            return null;
        }
        if ((status.state || 'stopped') === 'stopped') return null;

        const list = [{ id: 'cancel', name: 'Cancel', run: () => {} }];

        if (typeof status.shuffle === 'boolean') {
            list.push({
                id: 'shuffle',
                name: status.shuffle ? 'Shuffle off' : 'Shuffle on',
                run: () => fetch(`${base}/player/shuffle`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: !status.shuffle }),
                }),
            });
        }

        if (trackUri) {
            list.push({
                id: 'track_radio',
                name: 'Track radio',
                run: () => fetch(`${base}/player/play_track_radio`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ track_uri: trackUri }),
                }),
            });
        }

        return list.length > 1 ? list : null;  // Cancel alone = nothing to offer
    }

    function render() {
        const list = el?.querySelector('.pcm-list');
        if (!list) return;
        list.innerHTML = '';
        actions.forEach((a, i) => {
            const row = document.createElement('div');
            row.className = 'pcm-row';
            if (i === selectedIndex) row.classList.add('selected');
            row.textContent = a.name;
            list.appendChild(row);
        });
    }

    function resetIdle() {
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(close, TIMEOUT_MS);
    }

    // Bumped by close() so a close during open()'s fetch cancels the open —
    // otherwise a GO released mid-fetch would leave the menu popping up
    // afterwards with nobody holding.
    let openEpoch = 0;

    /** Open the menu (hold-GO fired). No-op when nothing is playing or the
     *  player is unreachable, mirroring the arc menu's silent decline. */
    async function open() {
        if (isOpen) return;
        const epoch = ++openEpoch;
        const list = await buildActions();
        if (epoch !== openEpoch) return;  // closed while fetching
        if (!list) return;
        actions = list;
        selectedIndex = 0;  // Cancel highlighted first, like the arc menu
        ensureEl();
        render();
        el.classList.add('visible');
        isOpen = true;
        resetIdle();
    }

    function close() {
        openEpoch++;  // cancels an in-flight open()
        if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
        if (el) el.classList.remove('visible');
        isOpen = false;
    }

    /** Nav wheel moves the highlight. Returns true = consumed. */
    function handleNav(data) {
        if (!isOpen || actions.length === 0) return false;
        resetIdle();
        const now = Date.now();
        if (now - lastStepAt < STEP_MIN_MS) return true;
        lastStepAt = now;
        const dir = data.direction === 'clock' ? 1 : -1;
        const next = Math.max(0, Math.min(actions.length - 1, selectedIndex + dir));
        if (next !== selectedIndex) { selectedIndex = next; render(); }
        return true;
    }

    /** GO released (or tapped while open) — run the highlighted action and
     *  close. Returns true = consumed. */
    async function executeSelected() {
        if (!isOpen) return false;
        const action = actions[selectedIndex];
        close();
        if (!action || action.id === 'cancel') return true;
        try {
            await action.run();
        } catch (e) {
            console.warn(`[PlayingContextMenu] ${action.id} failed:`, e);
        }
        return true;
    }

    /** Laser navigation dismisses, same as the speaker overlay. */
    function onLaserActivity() {
        if (isOpen) close();
    }

    return {
        get isOpen() { return isOpen; },
        open, close, handleNav, executeSelected, onLaserActivity,
    };
})();

if (typeof window !== 'undefined') window.PlayingContextMenu = PlayingContextMenu;
