/**
 * Music Assistant Source Preset -- iframe-based ArcList browser
 *
 * Browse mode uses softarc/music_assistant.html (ArcList V2 with lazy
 * loading of the MA library). Playing mode shows track info in the
 * standard PLAYING view; transport commands go to the MA source service.
 */

const _musicAssistantController = (() => {
    const MA_URL = () => window.AppConfig?.musicAssistantServiceUrl || 'http://localhost:8780';
    let _playing = false;

    // The controller is only consulted on the PLAYING page (as the active
    // source). The library views are separate iframe routes that own their own
    // nav/buttons directly — so here we only need PLAYING transport.
    return {
        get isActive() { return true; },

        updateMetadata(data) {
            _playing = (data.state === 'playing' || data.state === 'paused');
        },

        handleNavEvent() {
            return false;  // PLAYING wheel falls through to the main menu
        },

        handleButton(button) {
            if (!_playing) return false;
            const cmd = { go: 'toggle', left: 'prev', right: 'next', down: 'prev' }[button];
            if (!cmd) return false;
            fetch(`${MA_URL()}/command`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: cmd }),
            }).catch(() => {});
            return true;
        },
    };
})();

// The BeoSound 5 shows the library *views* in the left Arc (COVERS, ARTISTS,
// ALBUMS, TITLES, …). We mirror that: each MA browse category becomes a
// left-menu entry. They all share ONE preloaded arc-list iframe; picking one
// posts an 'open-path' message so the list shows that category as its root.
const MA_PRELOAD_ID = 'preload-music-assistant';
const MA_CONTAINER_ID = 'music-assistant-container';
const MA_IFRAME_SRC = 'softarc/music_assistant.html';

// Menu label → MA browse path. Order = order in the left Arc.
// `music_assistant.categories` in config (edited in the config UI's MUSIC
// card) selects which of these appear; absent/empty = all of them.
const MA_ALL_CATEGORIES = [
    { title: 'DISCOVER', path: 'menu/ma_discover', section: 'discover' },
    { title: 'ARTISTS', path: 'menu/ma_artists', section: 'artists' },
    { title: 'ALBUMS', path: 'menu/ma_albums', section: 'albums' },
    { title: 'PLAYLISTS', path: 'menu/ma_playlists', section: 'playlists' },
    { title: 'TRACKS', path: 'menu/ma_tracks', section: 'tracks' },
    { title: 'RADIO', path: 'menu/ma_radio', section: 'radios' },
];

const MA_CATEGORIES = (() => {
    const sel = window.AppConfig?.raw?.music_assistant?.categories;
    if (!Array.isArray(sel) || sel.length === 0) return MA_ALL_CATEGORIES;
    const enabled = new Set(sel);
    const picked = MA_ALL_CATEGORIES.filter(c => enabled.has(c.section));
    return picked.length ? picked : MA_ALL_CATEGORIES;
})();

// Opt these MA browse routes into the hold-GO context menu (hardware-input.js
// checks this Set before arming the hold timer). Only routes whose iframe
// understands 'context_open'/'go_release' — i.e. the MA arc-list — register.
window.ContextMenuRoutes = window.ContextMenuRoutes || new Set();
MA_CATEGORIES.forEach(cat => window.ContextMenuRoutes.add(cat.path));

/** Tell the shared MA iframe which category to show as its root list.
 *  Re-parenting the preloaded iframe on navigation reloads its document, so
 *  postMessage alone races the reload (messages land in the dying document).
 *  The section is therefore written to localStorage FIRST — same origin, so
 *  the iframe reads it on (re)load — and the messages only cover the
 *  already-loaded case (live switch without a reload). */
function _maOpenSection(section) {
    try { localStorage.setItem('ma_current_section', section); } catch (e) {}
    const send = () => {
        const f = document.getElementById(MA_PRELOAD_ID);
        if (f?.contentWindow) {
            f.contentWindow.postMessage({ type: 'open-path', path: section }, '*');
        }
    };
    send();
    setTimeout(send, 150);
    setTimeout(send, 400);
}

/** Build the shared view definition for one category. */
function _maCategoryView(cat) {
    return {
        title: cat.title,
        content: `<div id="${MA_CONTAINER_ID}" style="width:100%;height:100%;"></div>`,
        preloadId: MA_PRELOAD_ID,
        iframeSrc: MA_IFRAME_SRC,
        containerId: MA_CONTAINER_ID,
        // Fired by ViewManager after the iframe is (re)attached.
        onShow() { _maOpenSection(cat.section); },
    };
}

// -- Music Assistant Source Preset --
window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.music_assistant = {
    controller: _musicAssistantController,
    // Kept for controller/active-source lookups (keyed by preset id, not route);
    // the standalone "Music" entry is replaced by the category entries below.
    item: { title: 'Music', path: 'menu/music_assistant' },
    after: 'menu/playing',
    // Base view (still used as the iframe template / preload source).
    view: {
        title: 'Music',
        content: `<div id="${MA_CONTAINER_ID}" style="width:100%;height:100%;"></div>`,
        preloadId: MA_PRELOAD_ID,
        iframeSrc: MA_IFRAME_SRC,
        containerId: MA_CONTAINER_ID,
    },
    // Left-Arc view entries — expanded into individual menu items by MenuManager.
    categories: MA_CATEGORIES.map(cat => ({
        title: cat.title,
        path: cat.path,
        section: cat.section,
        view: _maCategoryView(cat),
    })),

    onAdd() {},
    onMount() {},
    onRemove() {},
};
