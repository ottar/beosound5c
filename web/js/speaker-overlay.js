// Speaker Overlay
//
// A lightweight, non-arc list of Music Assistant speakers shown as an
// overlay on the RIGHT of the PLAYING view / now-playing screensaver,
// sitting just left of the volume wheel/arc.
// Opened by a double-press of GO; the nav wheel scrolls the list and a
// single GO joins/leaves the highlighted speaker relative to the current
// target group (same /player/network + /player/join|unjoin the JOIN arc uses).
//
// It shows speaker NAMES only — no artwork, icons or playback info.
// Closes on laser-pointer navigation (main-menu move) or after
// join.overlay_timeout seconds with no nav-wheel activity. The volume
// wheel is never touched, so volume keeps working while it is open.

const SpeakerOverlay = (() => {
    const PLAYER_URL = () => (window.AppConfig?.playerUrl) || 'http://localhost:8766';

    let isOpen = false;
    let speakers = [];      // [{id, name, isTarget, inGroup, canJoin}]
    let selectedIndex = 0;
    let idleTimer = null;
    let timeoutMs = 15000;  // overridden from config.join.overlay_timeout
    let configLoaded = false;
    let el = null;          // overlay root element
    let lastStepAt = 0;     // throttle for nav-wheel scrolling
    const STEP_MIN_MS = 220; // one row step at most this often (anti-oversensitive)

    async function loadConfigOnce() {
        if (configLoaded) return;
        configLoaded = true;
        for (const path of ['/json/config.json', '/config/default.json']) {
            try {
                const resp = await fetch(path);
                if (!resp.ok) continue;
                const cfg = await resp.json();
                const secs = Number(cfg.join?.overlay_timeout);
                if (Number.isFinite(secs) && secs > 0) timeoutMs = secs * 1000;
                return;
            } catch { /* try next */ }
        }
    }

    function ensureEl() {
        if (el) return el;
        el = document.createElement('div');
        el.id = 'speaker-overlay';
        el.innerHTML = '<div class="spk-title">SPEAKERS</div><div class="spk-list"></div>' +
            '<div class="spk-help">GO — GROUP / UNGROUP<br>HOLD GO — PLAY HERE<br>' +
            'VOLUME WHEEL — SPEAKER LEVEL</div>';
        document.body.appendChild(el);
        return el;
    }

    async function fetchSpeakers() {
        const base = PLAYER_URL();
        const [netResp, statusResp] = await Promise.all([
            fetch(`${base}/player/network`),
            fetch(`${base}/player/status`),
        ]);
        if (!statusResp.ok) return null;
        const status = await statusResp.json();
        if ((status.player || '') !== 'music_assistant') return null;  // MA only
        if (!netResp.ok) return [];
        const net = await netResp.json();
        return net.map(d => ({
            id: d.id,
            name: d.name,
            isTarget: !!d.is_target,
            inGroup: !!d.in_group,
            canJoin: !!d.can_join,
            volume: (typeof d.volume === 'number') ? d.volume : null,
        }));
    }

    function render() {
        const list = el?.querySelector('.spk-list');
        if (!list) return;
        list.innerHTML = '';
        speakers.forEach((s, i) => {
            const row = document.createElement('div');
            row.className = 'spk-row';
            if (i === selectedIndex) row.classList.add('selected');
            // Target (the speaker others group TO) and grouped members show
            // bright; ungrouped speakers are dimmed.
            if (s.isTarget || s.inGroup) row.classList.add('active');

            // Status dot on the LEFT: target (grouped-to) / member
            // (grouped) / none — leads the row, before name + volume.
            const dot = document.createElement('span');
            dot.className = 'spk-dot';
            if (s.isTarget) dot.classList.add('target');
            else if (s.inGroup) dot.classList.add('member');
            row.appendChild(dot);

            // Name + volume stacked to the right of the dot.
            const content = document.createElement('div');
            content.className = 'spk-content';

            const name = document.createElement('span');
            name.className = 'spk-name';
            name.textContent = s.name;
            content.appendChild(name);

            // Per-speaker volume bar — the volume wheel trims the highlighted
            // row (individual volume), mirroring the JOIN arc view.
            if (s.volume != null) {
                const vol = document.createElement('div');
                vol.className = 'spk-vol';
                const fill = document.createElement('div');
                fill.className = 'spk-vol-fill';
                fill.style.width = `${s.volume}%`;
                vol.appendChild(fill);
                content.appendChild(vol);
            }

            row.appendChild(content);
            list.appendChild(row);
        });
    }

    function resetIdle() {
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(close, timeoutMs);
    }

    async function open() {
        if (isOpen) return;
        await loadConfigOnce();
        let list;
        try {
            list = await fetchSpeakers();
        } catch (e) {
            console.warn('[SpeakerOverlay] fetch failed:', e);
            return;
        }
        if (!list || list.length === 0) return;  // not MA, or no speakers
        speakers = list;
        // Start on the target speaker if present.
        const t = speakers.findIndex(s => s.isTarget);
        selectedIndex = t >= 0 ? t : 0;
        ensureEl();
        render();
        el.classList.add('visible');
        isOpen = true;
        resetIdle();
    }

    function close() {
        if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
        if (el) el.classList.remove('visible');
        isOpen = false;
    }

    /** Nav wheel scrolls the list (clock = down), one row per step. The nav
     *  wheel fires a burst of events per notch, so we time-throttle to keep
     *  it from racing through the list. Returns true = consumed. */
    function handleNav(data) {
        if (!isOpen || speakers.length === 0) return false;
        resetIdle();
        const now = Date.now();
        if (now - lastStepAt < STEP_MIN_MS) return true;  // swallow the burst
        lastStepAt = now;
        const dir = data.direction === 'clock' ? 1 : -1;
        const next = Math.max(0, Math.min(speakers.length - 1, selectedIndex + dir));
        if (next !== selectedIndex) { selectedIndex = next; render(); }
        return true;
    }

    /** Short GO joins/leaves the highlighted speaker (no-op on the target,
     *  which is the group anchor). Returns true = consumed. */
    async function handleGo() {
        if (!isOpen) return false;
        const s = speakers[selectedIndex];
        resetIdle();
        if (!s || s.isTarget) return true;
        const action = s.inGroup ? 'unjoin' : 'join';
        try {
            await fetch(`${PLAYER_URL()}/player/${action}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: s.id }),
            });
            // Optimistic flip, then refresh from the source of truth.
            s.inGroup = !s.inGroup;
            render();
            const fresh = await fetchSpeakers();
            if (fresh && fresh.length) {
                const keepId = speakers[selectedIndex]?.id;
                speakers = fresh;
                const idx = speakers.findIndex(x => x.id === keepId);
                if (idx >= 0) selectedIndex = idx;
                selectedIndex = Math.min(selectedIndex, speakers.length - 1);
                render();
            }
        } catch (e) {
            console.warn(`[SpeakerOverlay] ${action} failed:`, e);
        }
        return true;
    }

    /** Long GO: make the highlighted speaker the playback target ("play
     *  here") — the player service transfers the active queue to it.
     *  Returns true = consumed. */
    async function handleGoLong() {
        if (!isOpen) return false;
        const s = speakers[selectedIndex];
        resetIdle();
        if (!s || s.isTarget) return true;
        try {
            await fetch(`${PLAYER_URL()}/player/select_target`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: s.id }),
            });
            const fresh = await fetchSpeakers();
            if (fresh && fresh.length) {
                const keepId = s.id;
                speakers = fresh;
                const idx = speakers.findIndex(x => x.id === keepId);
                if (idx >= 0) selectedIndex = idx;
                render();
            }
        } catch (e) {
            console.warn('[SpeakerOverlay] select_target failed:', e);
        }
        return true;
    }

    /** Volume wheel trims the highlighted speaker's individual volume while
     *  the overlay is open (POST /player/member_volume, debounced). Returns
     *  true = consumed, so the master/group volume is left alone. */
    let memberVolTimer = null;
    function handleVolumeEvent(data) {
        if (!isOpen || speakers.length === 0) return false;
        const s = speakers[selectedIndex];
        if (!s || s.volume == null) return false;
        resetIdle();
        // Match the master volume wheel's feel (hardware-input.js): a fine,
        // non-linear step with fractional accumulation — the old
        // round(speed/10) made individual volume jump in big integer steps.
        const dir = data.direction === 'clock' ? 1 : -1;
        const scale = 1.5 - (s.volume / 100) * 0.9;
        s.volume = Math.max(0, Math.min(100, s.volume + dir * (data.speed || 10) / 14 * scale));
        // Live bar feedback (render() only runs on nav, so patch directly).
        const fill = el?.querySelector('.spk-row.selected .spk-vol-fill');
        if (fill) fill.style.width = `${s.volume}%`;
        if (memberVolTimer) clearTimeout(memberVolTimer);
        const id = s.id, vol = Math.round(s.volume);
        memberVolTimer = setTimeout(() => {
            memberVolTimer = null;
            fetch(`${PLAYER_URL()}/player/member_volume`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id, volume: vol }),
            }).catch(e => console.warn('[SpeakerOverlay] member_volume failed:', e));
        }, 50);
        return true;
    }

    /** Any laser-pointer movement (main-menu navigation) closes the overlay. */
    function onLaserActivity() {
        if (isOpen) close();
    }

    return {
        get isOpen() { return isOpen; },
        open, close, handleNav, handleGo, handleGoLong, handleVolumeEvent, onLaserActivity,
    };
})();

if (typeof window !== 'undefined') window.SpeakerOverlay = SpeakerOverlay;
