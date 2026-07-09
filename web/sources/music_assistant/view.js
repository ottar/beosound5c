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

    /** Try sending a message to the MA iframe. Returns true if sent. */
    function sendToIframe(type, data) {
        if (!window.IframeMessenger) return false;
        return IframeMessenger.sendToRoute('menu/music_assistant', type, data);
    }

    return {
        get isActive() { return true; },

        updateMetadata(data) {
            _playing = (data.state === 'playing' || data.state === 'paused');
        },

        handleNavEvent(data) {
            return sendToIframe('nav', { data });
        },

        handleButton(button) {
            // Try iframe first (browse page)
            if (sendToIframe('button', { button })) return true;
            // Iframe not mounted -> PLAYING page media controls
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

// -- Music Assistant Source Preset --
window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.music_assistant = {
    controller: _musicAssistantController,
    item: { title: 'Music', path: 'menu/music_assistant' },
    after: 'menu/playing',
    view: {
        title: 'Music',
        content: '<div id="music-assistant-container" style="width:100%;height:100%;"></div>',
        preloadId: 'preload-music-assistant',
        iframeSrc: 'softarc/music_assistant.html',
        containerId: 'music-assistant-container'
    },

    onAdd() {},

    onMount() {
        if (window.IframeMessenger) {
            IframeMessenger.registerIframe('menu/music_assistant', 'preload-music-assistant');
        }
        try {
            const iframe = document.getElementById('preload-music-assistant');
            const inst = iframe?.contentWindow?.arcListInstance;
            if (inst?.revive) inst.revive();
        } catch (e) { /* iframe not ready */ }
    },

    onRemove() {
        if (window.IframeMessenger) {
            IframeMessenger.unregisterIframe('menu/music_assistant');
        }
    },
};
