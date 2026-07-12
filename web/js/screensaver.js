// Screensaver
//
// Full-screen idle overlay for the kiosk. After `screensaver.timeout`
// minutes without hardware input (wheels, buttons, laser — ws-dispatcher
// calls touch() on every such event) it fades in the configured content:
//
//   clock   — large B&O-style clock + date
//   weather — clock + current temperature/conditions from /weather/forecast
//   covers  — album-art slideshow from the Music Assistant library
//   black   — plain black (LCD backlight stays on; real screen-off is the
//             router's auto-standby / the power button)
//
// It never covers active playback: while media is playing the PLAYING
// view's immersive mode is the screensaver, so activation is skipped and
// retried. Any input hides the overlay instantly. Config lives in
// /etc/beosound5c/config.json (`screensaver` section) via the symlinked
// /json/config.json, editable in the config UI.

const ScreenSaver = (() => {
    const MA_SOURCE_URL = () => `http://${location.hostname}:8780`;
    const RETRY_WHILE_PLAYING_MS = 60000;
    const COVER_ROTATE_MS = 20000;
    const CLOCK_DRIFT_PX = 40;   // burn-in avoidance: content drifts each minute

    let timeoutMs = 0;           // 0 = disabled
    let content = 'clock';
    let idleTimer = null;
    let visible = false;
    let el = null;
    let tickTimer = null;        // clock update / cover rotation
    let weatherTimer = null;
    let covers = [];
    let coverIdx = 0;

    async function loadConfig() {
        for (const path of ['/json/config.json', '/config/default.json']) {
            try {
                const resp = await fetch(path);
                if (!resp.ok) continue;
                const cfg = await resp.json();
                const ss = cfg.screensaver || {};
                const mins = Number(ss.timeout);
                timeoutMs = Number.isFinite(mins) && mins > 0 ? mins * 60000 : 0;
                if (['clock', 'weather', 'covers', 'black'].includes(ss.content)) {
                    content = ss.content;
                }
                return;
            } catch { /* try next */ }
        }
    }

    function isPlaying() {
        const s = window.uiStore?.mediaInfo?.state;
        return s === 'playing' || s === 'TRANSITIONING';
    }

    function ensureEl() {
        if (el) return el;
        el = document.createElement('div');
        el.id = 'screensaver-overlay';
        el.innerHTML = '<div class="ss-inner"></div>';
        document.body.appendChild(el);
        return el;
    }

    // ── Content renderers ──

    function fmtClock(now) {
        const t = now.toLocaleTimeString('nb-NO', { hour: '2-digit', minute: '2-digit' });
        const d = now.toLocaleDateString('nb-NO', { weekday: 'long', day: 'numeric', month: 'long' });
        return { t, d };
    }

    function drift(now) {
        // Deterministic slow drift keyed to the minute, ±CLOCK_DRIFT_PX.
        const m = now.getHours() * 60 + now.getMinutes();
        const x = Math.sin(m / 7) * CLOCK_DRIFT_PX;
        const y = Math.cos(m / 11) * CLOCK_DRIFT_PX;
        return `translate(${x.toFixed(0)}px, ${y.toFixed(0)}px)`;
    }

    function renderClock(inner) {
        const now = new Date();
        const { t, d } = fmtClock(now);
        inner.innerHTML =
            `<div class="ss-center" style="transform:${drift(now)}">` +
            `<div class="ss-time">${t}</div><div class="ss-date">${d}</div></div>`;
    }

    let weatherData = null;
    async function fetchWeather() {
        try {
            const resp = await fetch('/weather/forecast');
            if (resp.ok) weatherData = await resp.json();
        } catch { /* keep old */ }
    }

    function renderWeather(inner) {
        const now = new Date();
        const { t, d } = fmtClock(now);
        let wx = '';
        const ts = weatherData?.forecast?.properties?.timeseries?.[0];
        if (ts) {
            const inst = ts.data?.instant?.details || {};
            const sym = (ts.data?.next_1_hours?.summary?.symbol_code || '')
                .split('_')[0].replace(/([a-z])([A-Z])/g, '$1 $2');
            const temp = inst.air_temperature;
            wx = `<div class="ss-wx">${temp != null ? Math.round(temp) + '°' : ''}` +
                 `<span class="ss-wx-sym"> ${sym}</span>` +
                 `<span class="ss-wx-place"> · ${weatherData.name || ''}</span></div>`;
        }
        inner.innerHTML =
            `<div class="ss-center" style="transform:${drift(now)}">` +
            `<div class="ss-time">${t}</div><div class="ss-date">${d}</div>${wx}</div>`;
    }

    async function fetchCovers() {
        try {
            const resp = await fetch(`${MA_SOURCE_URL()}/browse?path=albums`,
                                     { signal: AbortSignal.timeout(10000) });
            if (!resp.ok) return;
            const data = await resp.json();
            covers = (data.items || []).filter(i => i.image);
            // Fisher–Yates shuffle so every idle session shows a fresh mix
            for (let i = covers.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [covers[i], covers[j]] = [covers[j], covers[i]];
            }
        } catch { /* covers stays empty → clock fallback */ }
    }

    function renderCover(inner) {
        if (!covers.length) { renderClock(inner); return; }
        const c = covers[coverIdx % covers.length];
        const now = new Date();
        const { t } = fmtClock(now);
        inner.innerHTML =
            `<div class="ss-cover-wrap">` +
            `<img class="ss-cover" src="${c.image}" alt="">` +
            `<div class="ss-cover-name">${c.name || ''}</div>` +
            `<div class="ss-cover-sub">${c.subtitle || ''}</div>` +
            `</div><div class="ss-corner-clock">${t}</div>`;
    }

    function renderCurrent() {
        const inner = el?.querySelector('.ss-inner');
        if (!inner) return;
        if (content === 'weather') renderWeather(inner);
        else if (content === 'covers') renderCover(inner);
        else if (content === 'black') inner.innerHTML = '';
        else renderClock(inner);
    }

    // ── Show / hide ──

    async function show() {
        if (visible) return;
        if (isPlaying()) {           // immersive mode owns playback idle
            armTimer(RETRY_WHILE_PLAYING_MS);
            return;
        }
        if (content === 'covers' && !covers.length) await fetchCovers();
        if (content === 'weather' && !weatherData) {
            await fetchWeather();
            if (weatherTimer) clearInterval(weatherTimer);
            weatherTimer = setInterval(fetchWeather, 30 * 60000);
        }
        ensureEl();
        coverIdx = 0;
        renderCurrent();
        el.classList.add('visible');
        visible = true;
        if (tickTimer) clearInterval(tickTimer);
        tickTimer = setInterval(() => {
            if (!visible) return;
            if (isPlaying()) { hide(); return; }   // playback started remotely
            if (content === 'covers') coverIdx++;
            renderCurrent();
        }, content === 'covers' ? COVER_ROTATE_MS : 20000);
    }

    function hide() {
        if (!visible) return;
        visible = false;
        el?.classList.remove('visible');
        if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
        if (weatherTimer) { clearInterval(weatherTimer); weatherTimer = null; }
        // Re-arm so the saver returns after the next idle period — without
        // this, a hide caused by remotely-started playback (the tick check)
        // would end the cycle permanently: touch() only runs on input.
        armTimer();
    }

    function armTimer(ms) {
        if (idleTimer) clearTimeout(idleTimer);
        if (!timeoutMs) return;
        idleTimer = setTimeout(show, ms ?? timeoutMs);
    }

    /** Called by ws-dispatcher on every hardware input event. */
    function touch() {
        if (visible) hide();
        armTimer();
    }

    loadConfig().then(() => armTimer());

    return {
        get isVisible() { return visible; },
        touch,
    };
})();

if (typeof window !== 'undefined') window.ScreenSaver = ScreenSaver;
