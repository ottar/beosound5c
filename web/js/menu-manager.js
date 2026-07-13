/**
 * MenuManager — owns the menu item list, rendering, and source script loading.
 *
 * Manages:
 *  - menuItems[] — the ordered list of {title, path} objects
 *  - views{} — route → {title, content, preloadId, ...} definitions
 *  - Source script loading and iframe preloading
 *  - Menu item DOM rendering (static + FLIP-animated)
 *
 * Requires:
 *  - window.Constants, window.arcs, window.LaserPositionMapper
 *  - this.onNavigate(path) callback — wired by UIStore
 *  - this.onMenuLoaded(data) callback — wired by UIStore
 */

class MenuManager {
    constructor() {
        const c = window.Constants || {};
        this.radius = c.arc?.radius || 1000;
        this.angleStep = c.arc?.menuAngleStep || 5;

        // Menu items from centralized constants (static views only — sources/webpages added by router)
        this.menuItems = (c.menuItems || [
            {title: 'PLAYING', path: 'menu/playing'},
            {title: 'SCENES', path: 'menu/scenes'},
            {title: 'SYSTEM', path: 'menu/system'},
            {title: 'SHOWING', path: 'menu/showing'}
        ]).map(item => ({title: item.title, path: item.path}));

        // View definitions keyed by route path
        this.views = {
            'menu/showing': {
                title: 'SHOWING',
                content: `
                    <div id="status-page" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; color: white; text-align: center; background-color: rgba(0,0,0,0.4);">
                        <div id="apple-tv-artwork-container" style="width: 60%; aspect-ratio: 1; margin: 20px; position: relative; display: flex; justify-content: center; align-items: center; overflow: hidden; border-radius: 8px; box-shadow: 0 5px 15px rgba(0,0,0,0.3);">
                            <img id="apple-tv-artwork" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" alt="Apple TV Media" style="width: 100%; height: 100%; object-fit: contain; transition: opacity 0.6s ease;">
                        </div>
                        <div id="apple-tv-media-info" style="width: 80%; padding: 10px;">
                            <div id="apple-tv-media-title" style="font-size: 24px; font-weight: bold; margin-bottom: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">—</div>
                            <div id="apple-tv-media-details" style="font-size: 18px; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">—</div>
                            <div id="apple-tv-state">Unknown</div>
                        </div>
                    </div>`
            },
            'menu/system': {
                title: 'System',
                content: `
                    <div id="system-container" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center;">
                        <iframe id="system-iframe" src="softarc/system.html" style="width: 100%; height: 100%; border: none;" allowfullscreen></iframe>
                    </div>
                `
            },
            'menu/scenes': {
                title: 'Scenes',
                content: `
                    <div id="scenes-container" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center;">
                    </div>
                `,
                preloadId: 'preload-scenes'
            },
            'menu/playing': {
                title: 'PLAYING',
                content: `
                    <div id="now-playing" class="media-view">
                        <div class="playing-artwork-slot media-view-artwork">
                            <div class="playing-flipper">
                                <div class="playing-face playing-front">
                                    <img class="playing-artwork" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" alt="Album Art">
                                </div>
                                <div class="playing-face playing-back" style="display:none">
                                    <img class="playing-artwork-back" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" alt="">
                                </div>
                            </div>
                        </div>
                        <div class="playing-info-slot media-view-info">
                            <div id="media-title" class="media-view-title">—</div>
                            <div id="media-artist" class="media-view-artist">—</div>
                            <div id="media-album" class="media-view-album">—</div>
                        </div>
                    </div>`
            },
        };

        this._menuLoaded = false;
        this._menuRetries = 0;
        this._lastSelectedPath = null;

        // Callbacks wired by UIStore
        this.onNavigate = null;      // (path) => void
        this.onMenuLoaded = null;    // (data) => void — called after _fetchMenu succeeds
    }

    // ── Router menu fetch ──

    async fetchMenu() {
        try {
            const resp = await fetch(`${window.AppConfig?.routerUrl || 'http://localhost:8770'}/router/menu`);
            const data = await resp.json();
            if (!data || !data.items) return;

            // Load source view scripts on demand
            for (const item of data.items) {
                if (item.dynamic && item.preset && !window.SourcePresets?.[item.preset]) {
                    await this.loadSourceScript(item.preset);
                }
            }

            // Rebuild menu items from router response
            const newItems = [];
            for (const item of data.items) {
                const path = `menu/${item.id}`;

                // Webpage items: iframe view (preserved across navigations via rescue logic)
                if (item.type === 'webpage' && item.url) {
                    const containerId = `webpage-container-${item.id}`;
                    this.views[path] = {
                        title: item.title,
                        content: `<div id="${containerId}" class="webpage-container" style="position:absolute;top:0;left:0;width:100%;height:100%;"></div>`,
                        _webpage: { iframeId: `preload-webpage-${item.id}`, containerId, url: item.url }
                    };
                    newItems.push({ title: item.title, path });
                }
                // Dynamic sources: always register view from preset (even if menu item already exists)
                else if (item.dynamic && item.preset) {
                    if (!window.SourcePresets?.[item.preset]) {
                        await this.loadSourceScript(item.preset);
                    }
                    const preset = window.SourcePresets?.[item.preset];
                    if (preset?.submenu || preset?.categories?.length) {
                        // Submenu-mode root entry (MA's single MUSIC item):
                        // laser-selecting it swaps the left menu to its items
                        // (see enterSubmenu). Views registered up front so
                        // the routes work as soon as the submenu opens.
                        if (preset.submenu) {
                            for (const cat of preset.submenu.items) {
                                this.views[cat.path] = cat.view || preset.view;
                                window.IframeMessenger?.registerIframe(cat.path, preset.view.preloadId);
                            }
                            newItems.push({
                                title: preset.submenu.title,
                                path: preset.submenu.path,
                                dynamic: true,
                                submenuItems: preset.submenu.items,
                            });
                        }
                        // Source exposes its browse categories as separate
                        // left-menu views (MA: DISCOVER/ARTISTS/ALBUMS/…), all
                        // sharing the one preloaded iframe.
                        for (const cat of preset.categories || []) {
                            newItems.push({ title: cat.title, path: cat.path, dynamic: true });
                            this.views[cat.path] = cat.view || preset.view;
                            window.IframeMessenger?.registerIframe(cat.path, preset.view.preloadId);
                        }
                    } else if (preset) {
                        newItems.push({ title: item.title, path: preset.item.path, dynamic: true });
                        if (preset.view) {
                            this.views[preset.item.path] = preset.view;
                        }
                    } else {
                        newItems.push({ title: item.title, path, dynamic: true });
                    }
                } else {
                    const existing = this.menuItems.find(m => m.path === path);
                    newItems.push(existing || { title: item.title, path });
                }
            }
            this.menuItems = newItems;
            this._rootMenuItems = null;   // a menu rebuild always exits any open submenu

            // Sync to laser position mapper
            if (window.LaserPositionMapper?.updateMenuItems) {
                window.LaserPositionMapper.updateMenuItems(this.menuItems);
            }

            this._menuLoaded = true;
            this._menuRetries = 0;

            // Notify UIStore so it can restore active source
            if (this.onMenuLoaded) this.onMenuLoaded(data);

            this.renderMenuItems();
            console.log(`[MENU] Loaded ${newItems.length} items from router (active: ${data.active_source || 'none'})`);
        } catch (e) {
            this._menuLoaded = true;
            const attempt = (this._menuRetries = (this._menuRetries || 0) + 1);
            if (attempt <= 15) {
                const delay = Math.min(attempt * 2000, 10000);
                console.log(`[MENU] Router unavailable, retrying in ${delay / 1000}s (attempt ${attempt})`);
                setTimeout(() => this.fetchMenu(), delay);
            } else {
                console.log('[MENU] Router unavailable after 15 attempts, using defaults');
            }
        }
    }

    /**
     * Dynamically load a source's view script (web/sources/{preset}/view.js).
     */
    loadSourceScript(preset) {
        return new Promise(resolve => {
            const existing = document.head.querySelector(`script[data-preset="${preset}"]`);
            if (existing) existing.remove();

            const script = document.createElement('script');
            script.src = `sources/${preset}/view.js`;
            script.dataset.preset = preset;
            script.onload = () => {
                console.log(`[MENU] Loaded source script: ${preset}`);
                const sp = window.SourcePresets?.[preset];
                if (sp?.view?.preloadId && sp.view.iframeSrc) {
                    this.preloadSourceIframe(sp.view.preloadId, sp.view.iframeSrc);
                    if (sp.item?.path) {
                        window.IframeMessenger?.registerIframe(sp.item.path, sp.view.preloadId);
                    }
                    // Category views (if any) share the same preloaded iframe.
                    for (const cat of sp.categories || []) {
                        window.IframeMessenger?.registerIframe(cat.path, sp.view.preloadId);
                    }
                }
                resolve();
            };
            script.onerror = () => {
                console.warn(`[MENU] Source script not found: ${preset}`);
                resolve();
            };
            document.head.appendChild(script);
        });
    }

    // ── Iframe preloading ──

    preloadIframes() {
        const iframesToPreload = [
            { id: 'preload-scenes', src: 'softarc/scenes.html' }
        ];

        let preloadContainer = document.getElementById('iframe-preload-container');
        if (!preloadContainer) {
            preloadContainer = document.createElement('div');
            preloadContainer.id = 'iframe-preload-container';
            preloadContainer.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;';
            document.body.appendChild(preloadContainer);
        }

        iframesToPreload.forEach(({ id, src }) => {
            if (!document.getElementById(id)) {
                const iframe = document.createElement('iframe');
                iframe.id = id;
                iframe.src = src;
                iframe.style.cssText = 'width:1024px;height:768px;border:none;';
                preloadContainer.appendChild(iframe);
                console.log(`[PRELOAD] Loading ${src}`);
            }
        });
    }

    preloadSourceIframe(id, src) {
        if (document.getElementById(id)) return;

        let preloadContainer = document.getElementById('iframe-preload-container');
        if (!preloadContainer) {
            preloadContainer = document.createElement('div');
            preloadContainer.id = 'iframe-preload-container';
            preloadContainer.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;';
            document.body.appendChild(preloadContainer);
        }

        const iframe = document.createElement('iframe');
        iframe.id = id;
        iframe.src = src;
        iframe.style.cssText = 'width:1024px;height:768px;border:none;';
        preloadContainer.appendChild(iframe);
        console.log(`[PRELOAD] Loading source iframe: ${src}`);
    }

    reloadAllSourceIframes() {
        for (const sp of Object.values(window.SourcePresets || {})) {
            if (sp.view?.preloadId) {
                const iframe = document.getElementById(sp.view.preloadId);
                if (iframe?.contentWindow) {
                    iframe.contentWindow.postMessage({ type: 'reload-data' }, '*');
                }
            }
        }
    }

    attachPreloadedIframe(preloadId) {
        let containerId = null;
        let iframeSrc = null;

        for (const sp of Object.values(window.SourcePresets || {})) {
            if (sp.view?.preloadId === preloadId) {
                containerId = sp.view.containerId;
                iframeSrc = sp.view.iframeSrc;
                break;
            }
        }

        if (!containerId) {
            const builtins = {
                'preload-scenes': { container: 'scenes-container', src: 'softarc/scenes.html' },
                'preload-security': { container: 'security-container', src: 'softarc/security.html' },
            };
            const b = builtins[preloadId];
            if (b) { containerId = b.container; iframeSrc = b.src; }
        }

        if (!containerId) {
            console.warn(`[PRELOAD] No mapping for ${preloadId}`);
            return;
        }

        const container = document.getElementById(containerId);
        if (!container) {
            console.warn(`[PRELOAD] Container ${containerId} not found`);
            return;
        }

        let iframe = container.querySelector('iframe');
        if (iframe) {
            console.log(`[PRELOAD] Iframe already in ${containerId}`);
            return;
        }

        iframe = document.getElementById(preloadId);
        if (iframe) {
            iframe.style.cssText = 'width: 100%; height: 100%; border: none; border-radius: 8px; box-shadow: 0 5px 15px rgba(0,0,0,0.3);';
            container.appendChild(iframe);
            console.log(`[PRELOAD] Attached ${preloadId} to ${containerId}`);
        } else if (iframeSrc) {
            iframe = document.createElement('iframe');
            iframe.id = preloadId;
            iframe.src = iframeSrc;
            iframe.style.cssText = 'width: 100%; height: 100%; border: none; border-radius: 8px; box-shadow: 0 5px 15px rgba(0,0,0,0.3);';
            container.appendChild(iframe);
            console.log(`[PRELOAD] Created fresh iframe for ${containerId}`);
        }

        // Resume any ArcList that was destroyed on the last nav-away. revive()
        // is idempotent (guards against double-start), so calling it on a
        // freshly-loaded iframe is safe — it's a no-op until destroy() runs.
        try {
            const inst = iframe?.contentWindow?.arcListInstance || iframe?.contentWindow?.arcList;
            if (inst?.revive) inst.revive();
        } catch (e) { /* iframe not ready / cross-origin */ }
    }

    // ── Rendering ──

    getStartItemAngle() {
        const visibleCount = this.menuItems.length;
        const totalSpan = this.angleStep * (visibleCount - 1);
        return 180 - totalSpan / 2;
    }

    _ensureHoverDelegation(menuContainer) {
        if (this._hoverDelegated) return;
        this._hoverDelegated = true;
        // Delegated mouseenter-equivalent: fires once when the pointer crosses
        // from outside a list-item (or from a different one) into this one.
        // Keeps a single listener on the container, so re-rendering items does
        // not leak per-item listeners on the detached nodes.
        menuContainer.addEventListener('mouseover', (e) => {
            const item = e.target.closest?.('.list-item');
            if (!item || !menuContainer.contains(item)) return;
            const from = e.relatedTarget?.closest?.('.list-item');
            if (item === from) return;
            const angle = parseFloat(item.dataset.angle);
            if (!Number.isNaN(angle) && this.onItemHover) this.onItemHover(angle);
        });
    }

    // ── Submenu (single MUSIC entry swaps the left menu) ──

    /** Laser landed on `path` — swap or restore the menu if it's a submenu
     *  trigger / the BACK entry. Returns true when the menu changed (the
     *  caller must then skip view navigation for this event). */
    handleMenuTrigger(path) {
        if (path === '__submenu_back') return this.exitSubmenu();
        const item = this.menuItems.find(m => m.path === path);
        if (item?.submenuItems?.length) return this.enterSubmenu(item);
        return false;
    }

    enterSubmenu(item) {
        if (this._rootMenuItems) return false;   // already inside one
        this._rootMenuItems = this.menuItems;
        this.menuItems = [
            { title: '‹ BACK', path: '__submenu_back' },
            ...item.submenuItems.map(c => ({ title: c.title, path: c.path, dynamic: true })),
        ];
        this._armSwapGuard();
        window.LaserPositionMapper?.updateMenuItems?.(this.menuItems);
        this.renderMenuItems();
        console.log(`[MENU] Entered submenu: ${item.title}`);
        return true;
    }

    exitSubmenu() {
        if (!this._rootMenuItems) return false;
        this.menuItems = this._rootMenuItems;
        this._rootMenuItems = null;
        this._armSwapGuard();
        window.LaserPositionMapper?.updateMenuItems?.(this.menuItems);
        this.renderMenuItems();
        console.log('[MENU] Exited submenu');
        return true;
    }

    // Swapping the menu changes every item's angle, so whatever slides in
    // under the stationary laser must not activate — with the root's MUSIC
    // and the submenu's '‹ BACK' at overlapping angles that instantly
    // re-toggled the swap, trapping the user (enter↔exit oscillation).
    _armSwapGuard() {
        this._swapGuardArmed = true;
        this._swapGuardPath = null;
        this._cancelTriggerHover();
    }

    // ── Dwell-to-activate for swap triggers ──
    //
    // Landing-activation is right for view navigation (cheap, transient)
    // but wrong for menu swaps: the pointer inevitably CROSSES the MUSIC
    // slot while traversing the root list, and a crossing must not swap
    // the menu. Triggers therefore fire only after the pointer RESTS on
    // them for TRIGGER_DWELL_MS. Timer-based, not event-based: the laser
    // stream goes silent when the hand is still, so the dwell must
    // complete without further events.
    TRIGGER_DWELL_MS = 400;

    _cancelTriggerHover() {
        if (this._hoverTimer) { clearTimeout(this._hoverTimer); this._hoverTimer = null; }
        this._hoverPath = null;
    }

    /** Called per wheel/laser event with the resolved menu path. Returns
     *  true when the path is a swap trigger (MUSIC / '‹ BACK') — the
     *  caller must then skip navigation; the swap itself fires from the
     *  dwell timer once the pointer has rested on it. */
    updateTriggerHover(path) {
        const item = this.menuItems.find(m => m.path === path);
        const isTrigger = path === '__submenu_back' || !!item?.submenuItems?.length;
        if (!isTrigger) {
            this._cancelTriggerHover();
            return false;
        }
        if (this._hoverPath !== path) {
            this._cancelTriggerHover();
            this._hoverPath = path;
            this._hoverTimer = setTimeout(() => {
                this._hoverTimer = null;
                const p = this._hoverPath;
                this._hoverPath = null;
                if (p && this.handleMenuTrigger(p)) {
                    window.uiStore?.sendClickCommand?.();
                }
            }, this.TRIGGER_DWELL_MS);
        }
        return true;
    }

    /** Called per wheel/laser event with the resolved menu path. Returns
     *  true while the selection under the pointer must be ignored: the
     *  first event after a swap adopts that item as "guarded", and it stays
     *  guarded until the pointer moves to a different item. */
    consumeSwapGuard(path) {
        if (this._swapGuardArmed) {
            this._swapGuardArmed = false;
            this._swapGuardPath = path;
            return true;
        }
        if (this._swapGuardPath) {
            if (path === this._swapGuardPath) return true;   // still resting on it
            this._swapGuardPath = null;                      // moved away — disarm
        }
        return false;
    }

    renderMenuItems() {
        const menuContainer = document.getElementById('menuItems');
        if (!menuContainer) return;
        this._ensureHoverDelegation(menuContainer);
        menuContainer.innerHTML = '';

        const visibleItems = this.menuItems;
        visibleItems.forEach((item, index) => {
            const itemElement = document.createElement('div');
            itemElement.className = 'list-item';
            itemElement.dataset.path = item.path;
            itemElement.textContent = item.title;

            const itemAngle = this.getStartItemAngle() + (visibleItems.length - 1 - index) * this.angleStep;
            const position = arcs.getArcPoint(this.radius, 20, itemAngle);
            itemElement.dataset.angle = String(itemAngle);

            Object.assign(itemElement.style, {
                position: 'absolute',
                left: `${position.x - 100}px`,
                top: `${position.y - 25}px`,
                width: '100px',
                height: '50px',
                cursor: 'pointer'
            });

            if (item.path === this._lastSelectedPath) {
                itemElement.classList.add('selectedItem');
            }

            menuContainer.appendChild(itemElement);
        });
    }

    /**
     * Bold the menu item at selectedIndex; click when selectedPath changes.
     */
    applyMenuHighlight(selectedIndex, selectedPath) {
        const menuContainer = document.getElementById('menuItems');
        if (!menuContainer) return;

        const menuElements = menuContainer.querySelectorAll('.list-item');
        menuElements.forEach((el, i) => {
            if (i === selectedIndex) {
                el.classList.add('selectedItem');
            } else {
                el.classList.remove('selectedItem');
            }
        });

        // Click exactly when the bolded item changes — one click per highlight change
        const changed = selectedPath && selectedPath !== this._lastSelectedPath;
        this._lastSelectedPath = selectedPath;
        return changed; // caller sends click command
    }

    // ── Dynamic add/remove ──

    addMenuItem(item, afterPath, viewDef) {
        if (this.menuItems.some(m => m.path === item.path)) {
            console.log(`[MENU] Item ${item.path} already exists`);
            return;
        }

        const afterIndex = this.menuItems.findIndex(m => m.path === afterPath);
        const insertAt = afterIndex !== -1 ? afterIndex + 1 : this.menuItems.length;
        const entry = { title: item.title, path: item.path, dynamic: true };
        if (item.submenuItems) entry.submenuItems = item.submenuItems;
        this.menuItems.splice(insertAt, 0, entry);

        if (viewDef) {
            this.views[item.path] = viewDef;
        }

        if (window.LaserPositionMapper?.updateMenuItems) {
            window.LaserPositionMapper.updateMenuItems(this.menuItems);
        }

        console.log(`[MENU] Added "${item.title}" after ${afterPath} (now ${this.menuItems.length} items)`);
        this.renderMenuItemsAnimated();
    }

    removeMenuItem(path) {
        const index = this.menuItems.findIndex(m => m.path === path);
        if (index === -1) {
            console.log(`[MENU] Item ${path} not found`);
            return;
        }

        // If currently viewing the removed item, navigate to adjacent
        if (this.onNavigate && this._currentRoute === path) {
            const adjacentPath = this.menuItems[index - 1]?.path || this.menuItems[index + 1]?.path || 'menu/playing';
            this.onNavigate(adjacentPath);
        }

        this.menuItems.splice(index, 1);
        delete this.views[path];

        if (window.LaserPositionMapper?.updateMenuItems) {
            window.LaserPositionMapper.updateMenuItems(this.menuItems);
        }

        console.log(`[MENU] Removed "${path}" (now ${this.menuItems.length} items)`);
        this.renderMenuItemsAnimated();
    }

    /**
     * Re-render menu items with FLIP animation.
     * Existing items slide to their new positions, new items fade in.
     */
    renderMenuItemsAnimated() {
        const menuContainer = document.getElementById('menuItems');
        if (!menuContainer) return;
        this._ensureHoverDelegation(menuContainer);

        // --- FIRST: record old positions keyed by data-path ---
        const oldPositions = {};
        menuContainer.querySelectorAll('.list-item[data-path]').forEach(el => {
            const rect = el.getBoundingClientRect();
            oldPositions[el.dataset.path] = { left: rect.left, top: rect.top };
        });

        // --- Rebuild DOM ---
        menuContainer.innerHTML = '';
        const visibleItems = this.menuItems;
        visibleItems.forEach((item, index) => {
            const itemElement = document.createElement('div');
            itemElement.className = 'list-item';
            itemElement.dataset.path = item.path;
            itemElement.textContent = item.title;

            const itemAngle = this.getStartItemAngle() + (visibleItems.length - 1 - index) * this.angleStep;
            const position = arcs.getArcPoint(this.radius, 20, itemAngle);
            itemElement.dataset.angle = String(itemAngle);

            Object.assign(itemElement.style, {
                position: 'absolute',
                left: `${position.x - 100}px`,
                top: `${position.y - 25}px`,
                width: '100px',
                height: '50px',
                cursor: 'pointer'
            });

            if (item.path === this._lastSelectedPath) {
                itemElement.classList.add('selectedItem');
            }

            menuContainer.appendChild(itemElement);
        });

        // --- LAST + INVERT + PLAY ---
        menuContainer.querySelectorAll('.list-item[data-path]').forEach(el => {
            const path = el.dataset.path;
            const newRect = el.getBoundingClientRect();

            if (oldPositions[path]) {
                const dx = oldPositions[path].left - newRect.left;
                const dy = oldPositions[path].top - newRect.top;
                if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
                    el.animate([
                        { transform: `translate(${dx}px, ${dy}px)` },
                        { transform: 'translate(0, 0)' }
                    ], { duration: 300, easing: 'ease-out' });
                }
            } else {
                el.animate([
                    { opacity: 0, transform: 'translateX(-20px)' },
                    { opacity: 1, transform: 'translateX(0)' }
                ], { duration: 300, delay: 150, easing: 'ease-out', fill: 'backwards' });
            }
        });
    }
}

window.MenuManager = MenuManager;
