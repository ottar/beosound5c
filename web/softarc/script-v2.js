/**
 * ArcList V2 - N-Level Hierarchical Arc Navigation
 *
 * Reusable component for circular arc-based list navigation with
 * unlimited depth levels. Uses a navStack for drill-down/back-out
 * with animated breadcrumbs.
 *
 * Config API:
 *   new ArcList({
 *     dataSource: 'url',        // URL to fetch JSON, or omit for inlineData
 *     inlineData: [...],        // Direct data array
 *     rootLoader: async () => [...],       // Async function returning root items (replaces dataSource/inlineData)
 *     childrenLoader: async (item, depth) => [...],  // Async function returning children (replaces childrenKey lookup)
 *     levels: [ { itemMapper, childrenKey, isContainer }, ... ],
 *     storagePrefix: 'prefix',
 *     context: 'spotify',
 *     webhookUrl: '...',
 *     onGo: (item, depth, pathContext, index) => {},
 *     webSocketUrl: '...',
 *   })
 */

// Neutral grey square shown while a row's artwork is pending (missing
// image, or deferred by the fast-scroll velocity gate).
const ARC_PLACEHOLDER_SRC =
    "data:image/svg+xml,%3Csvg width='128' height='128' xmlns='http://www.w3.org/2000/svg'%3E%3Crect width='128' height='128' fill='%23333'/%3E%3C/svg%3E";

class ArcList {
    constructor(config = {}) {
        // ===== CONFIGURATION =====
        this.config = {
            dataSource: config.dataSource || null,
            inlineData: config.inlineData || null,
            rootLoader: config.rootLoader || null,
            childrenLoader: config.childrenLoader || null,
            levels: config.levels || [{ isContainer: () => false }],
            storagePrefix: config.storagePrefix || 'arclist',
            context: config.context || 'generic',
            webhookUrl: config.webhookUrl || (typeof AppConfig !== 'undefined' ? AppConfig.webhookUrl : 'http://localhost:8767/forward'),
            webSocketUrl: config.webSocketUrl || (typeof AppConfig !== 'undefined' ? AppConfig.websocket.input : 'ws://localhost:8765'),
            onGo: config.onGo || null,
            // Hold-GO context menu (optional). contextMenuBuilder(item, depth,
            // pathContext) returns an array of final-shape action items
            // ({id,name,icon,color,run}) or null to decline. onContextAction
            // (optional) runs the chosen action; otherwise action.run is used.
            contextMenuBuilder: config.contextMenuBuilder || null,
            onContextAction: config.onContextAction || null,
        };

        // ===== ARC POSITIONING (from centralized Constants.softarc) =====
        const _sa = (window.parent?.Constants || window.Constants)?.softarc || {};
        this.SCROLL_SPEED = _sa.scrollSpeed ?? 0.5;
        this.SCROLL_STEP = _sa.scrollStep ?? 0.5;
        this.SNAP_DELAY = _sa.snapDelay ?? 1000;
        this.MIDDLE_INDEX = _sa.middleIndex ?? 4;
        this.BASE_X_OFFSET = _sa.baseXOffset ?? 100;
        this.MAX_RADIUS = _sa.maxRadius ?? 220;
        this.HORIZONTAL_MULTIPLIER = _sa.horizontalMultiplier ?? 0.35;
        this.BASE_ITEM_SIZE = _sa.baseItemSize ?? 128;

        // ===== BREADCRUMB POSITIONS =====
        this.BREADCRUMB_SLOTS = [
            { x: -320, imageSize: 128, showName: true, nameOpacity: 1, nameSize: '0.8rem', scale: 1 },
            { x: -430, imageSize: 80, showName: true, nameOpacity: 0.5, nameSize: '0.7rem', scale: 0.7 },
            { x: -500, imageSize: 64, showName: false, nameOpacity: 0, nameSize: '0.6rem', scale: 0.55 },
        ];

        // ===== STATE =====
        this.navStack = [];
        this.depth = 0;
        this.items = [];
        this.rootData = [];
        this.currentIndex = 0;
        this.targetIndex = 0;
        this.lastScrollTime = 0;
        this.animationFrame = null;
        this.lastClickedItemId = null;
        this.isAnimating = false;
        this._animationAbort = null;  // AbortController for interruptible animations
        this.snapTimer = null;
        this._inPageView = false;
        this._pageScrollEl = null;
        this._contextMenuOpen = false;

        // ===== DOM =====
        this.container = document.getElementById('arc-container');
        this.currentItemDisplay = document.getElementById('current-item');
        this.totalItemsDisplay = document.getElementById('total-items');
        this.counterPath = document.getElementById('counter-path');

        if (!this.container || !this.currentItemDisplay || !this.totalItemsDisplay) {
            console.error('Required DOM elements not found');
            return;
        }

        this.init();
    }

    // ─── LEVEL DESCRIPTORS ───────────────────────────────────────────

    /** Get the level descriptor for a given depth. Last one reused for deeper. */
    getLevelDescriptor(depth) {
        const levels = this.config.levels;
        return levels[Math.min(depth, levels.length - 1)];
    }

    /** Default item mapper — extracts standard fields from raw data */
    _defaultItemMapper(rawItem, index, allItems) {
        return {
            id: rawItem.id || `item-${index}`,
            name: rawItem.name || rawItem.title || `Item ${index + 1}`,
            image: rawItem.image || rawItem.thumbnail || null,
            icon: rawItem.icon || null,
            color: rawItem.color || null,
            ...rawItem, // preserve extra fields
        };
    }

    /** Map raw items through the level's itemMapper (or default) */
    mapItems(rawItems, depth) {
        const level = this.getLevelDescriptor(depth);
        const mapper = level.itemMapper || ((item, i, all) => this._defaultItemMapper(item, i, all));
        return rawItems.map((item, i, all) => mapper(item, i, all));
    }

    /** Check if a specific item is a container (has children to drill into) */
    isContainer(item) {
        const level = this.getLevelDescriptor(this.depth);
        return level.isContainer ? level.isContainer(item) : false;
    }

    /** Check if a specific item is a page (has page content to display) */
    isPage(item) { return !!(item.page); }

    /** Check if the current selected item can be drilled into */
    canDrillDown() {
        const idx = Math.round(this.currentIndex);
        const item = this.items[idx];
        if (!item) return false;
        return this.isContainer(item) || this.isPage(item);
    }

    // ─── INIT ────────────────────────────────────────────────────────

    async init() {
        await this.loadData();
        await this.restoreState();
        this.setupEventListeners();
        this.startAnimation();
        this.updateCounter();
        this.totalItemsDisplay.textContent = this.items.length;
        this.render();
    }

    // ─── DATA LOADING ────────────────────────────────────────────────

    async loadData() {
        try {
            let rawData;
            if (this.config.rootLoader) {
                rawData = await this.config.rootLoader();
            } else if (this.config.inlineData) {
                rawData = this.config.inlineData;
            } else if (this.config.dataSource) {
                const cacheBust = `${this.config.dataSource}${this.config.dataSource.includes('?') ? '&' : '?'}_=${Date.now()}`;
                const response = await fetch(cacheBust);
                rawData = await response.json();
            } else {
                rawData = [];
            }
            this.rootData = rawData;
            this.items = this.mapItems(rawData, 0);
            console.log('Loaded', this.items.length, 'items');
        } catch (error) {
            console.error('Error loading data:', error);
            this.rootData = [];
            this.items = [{ id: 'error', name: 'Error Loading Data', icon: 'warning', color: '#ff4444' }];
        }
    }

    async reloadData() {
        this._contextMenuOpen = false;
        if (this.depth !== 0) {
            console.log('[RELOAD] Skipped — currently at depth', this.depth);
            return;
        }
        const prevIndex = Math.round(this.currentIndex);
        const prevId = this.items[prevIndex]?.id;

        await this.loadData();

        if (prevId) {
            const newIndex = this.items.findIndex(item => item.id === prevId);
            if (newIndex >= 0) {
                this.currentIndex = newIndex;
                this.targetIndex = newIndex;
            }
        }
        if (this.currentIndex >= this.items.length) {
            this.currentIndex = Math.max(0, this.items.length - 1);
            this.targetIndex = this.currentIndex;
        }

        this.totalItemsDisplay.textContent = this.items.length;
        this.updateCounter();
        this.render();
        console.log('[RELOAD] Data refreshed');
    }

    /** Reset to depth 0 and reload the root list via rootLoader. Clears any
     *  drill state, breadcrumbs and hierarchy background. Used when an
     *  external caller swaps the root section (e.g. picking a MUSIC view from
     *  the left menu) — unlike reloadData(), it works from any depth. */
    async jumpToRoot() {
        this._abortAnimation();
        if (this.snapTimer) { clearTimeout(this.snapTimer); this.snapTimer = null; }
        this.navStack = [];
        this.depth = 0;
        this._inPageView = false;
        this._pageScrollEl = null;
        this._contextMenuOpen = false;
        this.currentIndex = 0;
        this.targetIndex = 0;
        this.container
            .querySelectorAll('.arc-item.breadcrumb, .arc-item.parent-hidden, .page-view')
            .forEach(el => el.remove());
        const bg = document.getElementById('hierarchy-background');
        if (bg) { bg.classList.remove('active'); bg.style.opacity = 0; }
        await this.loadData();
        this.currentIndex = 0;
        this.targetIndex = 0;
        this.totalItemsDisplay.textContent = this.items.length;
        this.updateCounter();
        this.render();
        this.saveState();
    }

    // ─── STATE PERSISTENCE ───────────────────────────────────────────

    get STORAGE_KEY() { return `${this.config.storagePrefix}_nav_state`; }

    saveState() {
        try {
            let depth = this.depth;
            let currentIndex = this.currentIndex;
            let stackFrames = this.navStack;

            // When in page view, save as parent level scrolled to the page item
            if (this._inPageView) {
                const pageFrame = stackFrames[stackFrames.length - 1];
                depth = this.depth - 1;
                currentIndex = pageFrame ? pageFrame.selectedIndex : 0;
                stackFrames = stackFrames.slice(0, -1);
            } else if (this._contextMenuOpen) {
                // The context menu is a transient overlay, not a browse level —
                // persist as the origin level scrolled to the acted-on item, so
                // a reload (saveState runs every 1s) restores the list, never
                // tries to rebuild the menu through childrenLoader.
                const menuFrame = stackFrames[stackFrames.length - 1];
                depth = this.depth - 1;
                currentIndex = menuFrame ? menuFrame.selectedIndex : 0;
                stackFrames = stackFrames.slice(0, -1);
            }

            const state = {
                version: 2,
                depth: depth,
                currentIndex: currentIndex,
                stack: stackFrames.map(frame => ({
                    selectedIndex: frame.selectedIndex,
                    selectedItemId: frame.selectedItem?.id || null,
                })),
            };
            localStorage.setItem(this.STORAGE_KEY, JSON.stringify(state));
        } catch (e) {
            console.error('Error saving state:', e);
        }
    }

    async restoreState() {
        try {
            const raw = localStorage.getItem(this.STORAGE_KEY);
            if (!raw) return;
            const state = JSON.parse(raw);
            if (state.version !== 2) return;

            // With childrenLoader, re-fetch each saved level asynchronously so
            // the user returns to exactly where they were drilled in (e.g.
            // MUSIC → artist → album), not just the root.
            if (this.config.childrenLoader) {
                await this._restoreViaLoader(state);
                return;
            }

            if (!state.stack) return;

            // Walk the stack: drill down silently through each saved level
            let currentItems = this.rootData;
            for (const frame of state.stack) {
                const level = this.getLevelDescriptor(this.depth);
                const mappedItems = this.mapItems(currentItems, this.depth);

                // Find the selected item
                let idx = mappedItems.findIndex(item => item.id === frame.selectedItemId);
                if (idx < 0) idx = Math.min(frame.selectedIndex, mappedItems.length - 1);
                if (idx < 0) break; // data changed, stop here

                const rawItem = currentItems[idx];
                const selectedItem = mappedItems[idx];
                const childrenKey = this._resolveChildrenKey(level, rawItem);
                const children = childrenKey ? rawItem[childrenKey] : null;
                if (!children || children.length === 0) break;

                // Push stack frame (silent, no animation)
                this.navStack.push({
                    items: mappedItems,
                    rawItems: currentItems,
                    selectedIndex: idx,
                    selectedItem: selectedItem,
                    breadcrumbElement: null,
                });
                this.depth++;
                currentItems = children;
            }

            // Set current items to the final level
            this.items = this.mapItems(currentItems, this.depth);
            this.currentIndex = Math.max(0, Math.min(this.items.length - 1, state.currentIndex));
            this.targetIndex = this.currentIndex;

            // Create breadcrumb DOM elements without animation
            this._createBreadcrumbsFromStack();

            // Activate hierarchy background if drilled in
            if (this.depth > 0) {
                const bg = document.getElementById('hierarchy-background');
                if (bg) {
                    bg.classList.add('active');
                    bg.style.opacity = Math.min(this.depth * 0.3, 0.8);
                }
            }

            this.totalItemsDisplay.textContent = this.items.length;
            console.log('Restored state at depth', this.depth);
        } catch (e) {
            console.error('Error restoring state:', e);
        }
    }

    /** Restore a drilled-in position for childrenLoader-backed lists by
     * re-fetching each saved level in turn (mirrors drillDown, but silent —
     * no animation). Stops early if the saved item is gone or the data
     * changed, leaving the user at the deepest level that still resolves. */
    async _restoreViaLoader(state) {
        // Depth 0 (or no saved stack): just restore the root scroll position.
        if (!state.stack || state.stack.length === 0) {
            this.currentIndex = Math.max(0, Math.min(this.items.length - 1, state.currentIndex));
            this.targetIndex = this.currentIndex;
            return;
        }

        for (const frame of state.stack) {
            const mappedItems = this.items;  // current level, already mapped
            let idx = mappedItems.findIndex(it => it.id === frame.selectedItemId);
            if (idx < 0) idx = Math.min(frame.selectedIndex, mappedItems.length - 1);
            if (idx < 0) break;

            const item = mappedItems[idx];
            if (!this.isContainer(item)) break;

            let rawChildren;
            try {
                rawChildren = await this.config.childrenLoader(item, this.depth);
            } catch (e) {
                console.warn('Restore: childrenLoader failed, stopping at depth', this.depth, e);
                break;
            }
            if (!rawChildren || rawChildren.length === 0) break;

            this.navStack.push({
                items: this.items,
                rawItems: this._getCurrentRawItems(),
                loadedChildren: rawChildren,
                selectedIndex: idx,
                selectedItem: item,
                breadcrumbElement: null,
            });
            this.items = this.mapItems(rawChildren, this.depth + 1);
            this.depth++;
        }

        // Restore scroll position within the deepest resolved level.
        this.currentIndex = Math.max(0, Math.min(this.items.length - 1, state.currentIndex));
        this.targetIndex = this.currentIndex;

        this._createBreadcrumbsFromStack();
        if (this.depth > 0) {
            const bg = document.getElementById('hierarchy-background');
            if (bg) {
                bg.classList.add('active');
                bg.style.opacity = Math.min(this.depth * 0.3, 0.8);
            }
        }
        this.totalItemsDisplay.textContent = this.items.length;
        console.log('Restored (loader) at depth', this.depth);
    }

    // ─── NAVIGATION: DRILL DOWN / GO BACK ────────────────────────────

    async drillDown() {
        this.snapToNearest();

        const idx = Math.round(this.currentIndex);
        const item = this.items[idx];
        const level = this.getLevelDescriptor(this.depth);

        // Page items get their own drill-in path
        if (this.isPage(item)) {
            this._drillIntoPage(idx, item);
            return;
        }

        if (!level.isContainer || !level.isContainer(item)) return;

        // Load children: async loader or inline childrenKey
        let rawChildren;
        if (this.config.childrenLoader) {
            rawChildren = await this.config.childrenLoader(item, this.depth);
        } else {
            const childrenKey = this._resolveChildrenKey(level, item);
            rawChildren = childrenKey ? item[childrenKey] : null;
        }
        if (!rawChildren || rawChildren.length === 0) return;

        // Interrupt any running animation
        this._abortAnimation();
        this._pauseAnimation();
        this._animationAbort = new AbortController();
        this.isAnimating = true;

        try {
            // Find the selected DOM element for animation
            const selectedElement = document.querySelector('.arc-item.selected:not(.breadcrumb)');

            // Push current state onto nav stack
            const frame = {
                items: this.items,
                rawItems: this._getCurrentRawItems(),
                loadedChildren: rawChildren,
                selectedIndex: idx,
                selectedItem: item,
                breadcrumbElement: null,
            };
            this.navStack.push(frame);

            // Load children
            const nextDepth = this.depth + 1;
            this.items = this.mapItems(rawChildren, nextDepth);
            this.depth = nextDepth;
            this.currentIndex = 0;
            this.targetIndex = 0;

            // Animate (delays resolve instantly if aborted mid-flight)
            await this._animateForward(frame, selectedElement);

            // Finalize
            this.isAnimating = false;
            this.render();
            this.updateCounter();
            this.totalItemsDisplay.textContent = this.items.length;
            this.saveState();
        } catch (e) {
            console.error('Error in drillDown:', e);
        } finally {
            this.isAnimating = false;
            this._animationAbort = null;
            this._resumeAnimation();
        }
    }

    async goBack() {
        if (this.depth === 0) return;

        // Interrupt any running animation
        this._abortAnimation();
        this.snapToNearest();
        this._pauseAnimation();
        this._animationAbort = new AbortController();
        this.isAnimating = true;

        try {
            const frame = this.navStack.pop();
            this.depth--;

            // Clean up page view before backward animation
            if (frame.isPageView) {
                this._inPageView = false;
                this._pageScrollEl = null;
                const pageEl = this.container.querySelector('.page-view');
                if (pageEl) {
                    pageEl.style.transition = 'opacity 300ms ease';
                    pageEl.style.opacity = '0';
                    await this.delay(300);
                    pageEl.remove();
                }
            }

            await this._animateBackward(frame);

            this.items = frame.items;
            this.currentIndex = frame.selectedIndex;
            this.targetIndex = frame.selectedIndex;

            // Finalize
            this.isAnimating = false;
            this.render();
            this.updateCounter();
            this.totalItemsDisplay.textContent = this.items.length;
            this.saveState();
        } catch (e) {
            console.error('Error in goBack:', e);
            // Emergency: restore from stack
            if (this.navStack.length === 0) {
                this.depth = 0;
                this.items = this.mapItems(this.rootData, 0);
            }
        } finally {
            this.isAnimating = false;
            this._animationAbort = null;
            this._resumeAnimation();
        }
    }

    // ─── CONTEXT MENU (hold-GO) ─────────────────────────────────────

    /** Open the hold-GO action menu for the highlighted item. Pushes a nav
     *  frame like drillDown (so the acted-on item slides into breadcrumb slot 0
     *  and the list is restored on close) but replaces the arc with the action
     *  list built by config.contextMenuBuilder. Declines silently when there's
     *  no builder, an animation is running, we're in a page view, the menu is
     *  already open, or the builder returns no actions (e.g. on a header). */
    async openContextMenu() {
        if (!this.config.contextMenuBuilder) return;
        if (this.isAnimating || this._inPageView || this._contextMenuOpen) return;

        this.snapToNearest();
        const idx = Math.round(this.currentIndex);
        const item = this.items[idx];
        if (!item) return;

        let actions;
        try {
            const pathContext = this.navStack.map(f => f.selectedItem);
            actions = this.config.contextMenuBuilder(item, this.depth, pathContext);
        } catch (e) {
            console.error('contextMenuBuilder failed:', e);
            return;
        }
        if (!actions || actions.length === 0) return;  // nothing actionable

        // Interrupt any running animation (mirror drillDown)
        this._abortAnimation();
        this._pauseAnimation();
        this._animationAbort = new AbortController();
        this.isAnimating = true;
        this._contextMenuOpen = true;

        try {
            const selectedElement = document.querySelector('.arc-item.selected:not(.breadcrumb)');
            const frame = {
                items: this.items,
                rawItems: this._getCurrentRawItems(),
                selectedIndex: idx,
                selectedItem: item,
                breadcrumbElement: null,
                isContextMenu: true,
            };
            this.navStack.push(frame);

            this.depth++;
            // Actions are already final-shape ({id,name,icon,color,run}); do
            // NOT run them through mapItems.
            this.items = actions;
            this.currentIndex = 0;   // Cancel (index 0) highlighted initially
            this.targetIndex = 0;

            await this._animateForward(frame, selectedElement);

            this.isAnimating = false;
            this.render();
            this.updateCounter();
            this.totalItemsDisplay.textContent = this.items.length;
        } catch (e) {
            console.error('Error in openContextMenu:', e);
        } finally {
            this.isAnimating = false;
            this._animationAbort = null;
            this._resumeAnimation();
        }
    }

    /** Close the menu, restoring the browse list at the same highlight. */
    closeContextMenu() {
        if (!this._contextMenuOpen) return;
        this._contextMenuOpen = false;
        return this.goBack();
    }

    /** GO released — run the highlighted action (or fall back to a plain GO
     *  when no menu is open, matching today's go_long→go = play). Cancel or an
     *  empty slot just closes. A navigation action turns the menu into a browse
     *  level via replaceContextMenuLevel() (which clears _contextMenuOpen), so
     *  it must NOT be closed with goBack afterwards. */
    async executeContextRelease() {
        if (!this._contextMenuOpen) {
            this.handleGo();
            return;
        }
        this.snapToNearest();
        const idx = Math.round(this.currentIndex);
        const action = this.items[idx];
        const frame = this.navStack[this.navStack.length - 1];
        const actedItem = frame ? frame.selectedItem : null;

        if (!action || action.id === 'cancel') {
            this.closeContextMenu();
            return;
        }

        const el = this.container.querySelector('.arc-item.selected');
        if (el) {
            el.classList.add('go-flash');
            setTimeout(() => el.classList.remove('go-flash'), 400);
        }

        try {
            if (this.config.onContextAction) {
                await this.config.onContextAction(action, actedItem, this);
            } else if (typeof action.run === 'function') {
                await action.run(actedItem, this);
            }
        } catch (e) {
            console.error('Context action failed:', e);
        }

        // Still open ⇒ a terminal action (play/favorite/…) ran; close it. If a
        // navigation action already replaced the level, _contextMenuOpen is
        // false and we leave the new browse level in place.
        if (this._contextMenuOpen) this.closeContextMenu();
    }

    /** Turn the open menu into a normal browse level (Go to artist/album):
     *  keep the nav frame (breadcrumb + return highlight) but swap the arc from
     *  actions to real children. Further drilling and › back to the origin work
     *  like any level. rawChildren are pre-mapped (mapMaItem), like loader
     *  results — mapItems (default passthrough) is applied for consistency. */
    replaceContextMenuLevel(rawChildren) {
        if (!this._contextMenuOpen) return;
        this._contextMenuOpen = false;
        const frame = this.navStack[this.navStack.length - 1];
        if (frame) frame.loadedChildren = rawChildren;
        this.items = this.mapItems(rawChildren || [], this.depth);
        this.currentIndex = 0;
        this.targetIndex = 0;
        this.totalItemsDisplay.textContent = this.items.length;
        this.render();
        this.updateCounter();
        this.saveState();
    }

    // ─── PAGE VIEW ──────────────────────────────────────────────────

    async _drillIntoPage(idx, item) {
        this._abortAnimation();
        this._pauseAnimation();
        this._animationAbort = new AbortController();
        this.isAnimating = true;

        try {
            const selectedElement = document.querySelector('.arc-item.selected:not(.breadcrumb)');

            const frame = {
                items: this.items,
                rawItems: this._getCurrentRawItems(),
                selectedIndex: idx,
                selectedItem: item,
                breadcrumbElement: null,
                isPageView: true,
            };
            this.navStack.push(frame);

            this.depth++;
            this.items = [];
            this._inPageView = true;

            // Phases 1–4: shared drill animation
            await this._animateDrillCommon(frame, selectedElement);

            // Phase 5: render page view and fade in
            const pageEl = this._renderPageView(item.page);
            await this.delay(50);
            pageEl.style.transition = 'opacity 300ms ease';
            pageEl.style.opacity = '1';
            await this.delay(300);

            this.isAnimating = false;
            this.updateCounter();
            this.saveState();
        } catch (e) {
            console.error('Error in _drillIntoPage:', e);
        } finally {
            this.isAnimating = false;
            this._animationAbort = null;
            this._resumeAnimation();
        }
    }

    _renderPageView(page) {
        const el = document.createElement('div');
        el.className = 'page-view';
        el.style.opacity = '0';

        const scroll = document.createElement('div');
        scroll.className = 'page-view-scroll';

        const title = document.createElement('h2');
        title.className = 'page-view-title';
        title.textContent = page.title;

        const body = document.createElement('div');
        body.className = 'page-view-body';
        body.innerHTML = page.body;

        scroll.appendChild(title);
        scroll.appendChild(body);
        el.appendChild(scroll);
        this.container.appendChild(el);
        this._pageScrollEl = scroll;
        return el;
    }

    /** Resolve childrenKey — supports string or function(item) */
    _resolveChildrenKey(level, item) {
        const key = level.childrenKey;
        if (typeof key === 'function') return key(item);
        return key;
    }

    /** Get the raw (un-mapped) items for the current depth */
    _getCurrentRawItems() {
        if (this.depth === 0) return this.rootData;
        const parentFrame = this.navStack[this.navStack.length - 1];
        if (!parentFrame) return this.rootData;
        // Lazy-loaded children stored on stack frame
        if (parentFrame.loadedChildren) return parentFrame.loadedChildren;
        const parentLevel = this.getLevelDescriptor(this.depth - 1);
        const parentRawItem = parentFrame.rawItems[parentFrame.selectedIndex];
        const key = this._resolveChildrenKey(parentLevel, parentRawItem);
        return key ? (parentRawItem[key] || []) : [];
    }

    // ─── ANIMATION ───────────────────────────────────────────────────

    /** Shared phases 1–4 of drill animation: background, breadcrumb shift, selected→breadcrumb, sibling fade, cleanup */
    async _animateDrillCommon(frame, selectedElement) {
        // Phase 1: Activate/deepen hierarchy background
        const bg = document.getElementById('hierarchy-background');
        if (bg) {
            bg.classList.add('active');
            bg.style.transition = 'opacity 250ms ease-out';
            bg.style.opacity = Math.min(this.depth * 0.3, 0.8);
        }

        // Phase 2: Shift existing breadcrumbs left by one slot
        this._shiftBreadcrumbsLeft();

        if (selectedElement) {
            // Phase 3: Animate selected item to breadcrumb slot 0
            const rect = selectedElement.getBoundingClientRect();
            const containerRect = this.container.getBoundingClientRect();
            const currentX = rect.left + rect.width / 2 - (containerRect.left + containerRect.width / 2);

            selectedElement.style.position = 'absolute';
            selectedElement.style.top = '50%';
            selectedElement.style.left = '50%';
            selectedElement.style.marginLeft = '-140px';
            selectedElement.style.marginTop = '-64px';
            selectedElement.style.zIndex = '10';
            selectedElement.style.pointerEvents = 'auto';
            selectedElement.style.transform = `translate(${currentX}px, 0px) scale(1)`;
            selectedElement.style.transition = 'none';
            selectedElement.classList.add('breadcrumb');
            selectedElement.classList.remove('selected');
            selectedElement.dataset.breadcrumbDepth = String(this.depth - 1);
            selectedElement.dataset.slotIndex = '0';
            selectedElement.offsetHeight; // force reflow

            // Hide sibling items
            const siblings = this.container.querySelectorAll('.arc-item:not(.breadcrumb)');
            siblings.forEach(item => {
                item.classList.add('parent-hidden');
                item.style.transition = 'opacity 300ms cubic-bezier(0.25, 0.46, 0.45, 0.94)';
                item.style.setProperty('opacity', '0', 'important');
            });

            const slot = this.BREADCRUMB_SLOTS[0];
            selectedElement.style.transition = 'transform 250ms cubic-bezier(0.25, 0.46, 0.45, 0.94), opacity 250ms cubic-bezier(0.25, 0.46, 0.45, 0.94)';
            selectedElement.style.transform = `translate(${slot.x}px, 0px) scale(${slot.scale})`;
            selectedElement.style.opacity = '1';
            selectedElement.style.filter = 'blur(0px)';

            frame.breadcrumbElement = selectedElement;

            await this.delay(400);
        } else {
            // No element to animate — create static breadcrumb
            frame.breadcrumbElement = this._createStaticBreadcrumb(frame.selectedItem, 0);

            const siblings = this.container.querySelectorAll('.arc-item:not(.breadcrumb)');
            siblings.forEach(item => {
                item.classList.add('parent-hidden');
                item.style.transition = 'opacity 300ms cubic-bezier(0.25, 0.46, 0.45, 0.94)';
                item.style.setProperty('opacity', '0', 'important');
            });
            await this.delay(300);
        }

        // Phase 4: Remove hidden parent items from DOM
        this.container.querySelectorAll('.arc-item.parent-hidden').forEach(el => el.remove());
    }

    async _animateForward(frame, selectedElement) {
        await this._animateDrillCommon(frame, selectedElement);

        // Phase 5: Render new children then fade them in
        this.isAnimating = false;
        this.render();
        this.isAnimating = true;

        // Small delay to ensure children are rendered and parents hidden
        await this.delay(100);

        const childElements = this.container.querySelectorAll('.arc-item:not(.breadcrumb)');
        childElements.forEach((el, i) => {
            el.style.setProperty('opacity', '0', 'important');
            el.style.transition = `opacity 300ms cubic-bezier(0.25, 0.46, 0.45, 0.94)`;
            el.style.transitionDelay = `${i * 50}ms`;
        });
        // Trigger fade-in
        await this.delay(50);
        childElements.forEach(el => {
            el.style.setProperty('opacity', '1', 'important');
        });
        await this.delay(400);
        // Clean up transition styles
        childElements.forEach(el => {
            el.style.transition = '';
            el.style.transitionDelay = '';
        });
    }

    async _animateBackward(frame) {
        // Phase 1: Fade out current items
        const currentItems = this.container.querySelectorAll('.arc-item:not(.breadcrumb)');
        currentItems.forEach((el, i) => {
            el.classList.add('track-exit');
        });
        await this.delay(300);
        currentItems.forEach(el => el.remove());

        // Phase 2: Slide breadcrumb slot 0 back to arc center
        const bc = frame.breadcrumbElement;
        if (bc && document.contains(bc)) {
            bc.classList.remove('breadcrumb');
            bc.classList.add('breadcrumb-slide-right', 'selected');
            bc.style.transition = 'none';
            bc.offsetHeight; // force reflow with current position
            // Apply target via rAF so transition triggers properly (matches V1)
            requestAnimationFrame(() => {
                bc.style.transition = 'transform 400ms cubic-bezier(0.25, 0.46, 0.45, 0.94), opacity 400ms cubic-bezier(0.25, 0.46, 0.45, 0.94)';
                bc.style.transform = `translate(${this.BASE_X_OFFSET}px, 0px) scale(1)`;
            });
            bc.style.opacity = '1';
            bc.style.filter = 'none';
        }

        // Phase 3: Shift remaining breadcrumbs right by one slot
        this._shiftBreadcrumbsRight();

        await this.delay(400);

        // Phase 4: Lighten hierarchy background
        const bg = document.getElementById('hierarchy-background');
        if (bg) {
            if (this.depth === 0) {
                bg.classList.remove('active');
                bg.style.opacity = 0;
            } else {
                bg.style.opacity = Math.min(this.depth * 0.3, 0.8);
            }
        }

        // Phase 5: Clean up breadcrumb element -> becomes regular item
        if (bc && document.contains(bc)) {
            bc.classList.remove('breadcrumb-slide-right');
            bc.style.transition = '';
            delete bc.dataset.breadcrumbDepth;
            delete bc.dataset.slotIndex;

            const nameEl = bc.querySelector('.item-name');
            if (nameEl) {
                nameEl.classList.add('selected');
                nameEl.classList.remove('unselected');
            }
        }

        // Phase 6: Render parent items (render() will replace all non-breadcrumb items)
        // The breadcrumb element will be cleaned up by the full render
        if (bc && document.contains(bc)) bc.remove();
    }

    /** Shift all existing breadcrumbs one slot to the left (deeper) */
    _shiftBreadcrumbsLeft() {
        const breadcrumbs = this.container.querySelectorAll('.arc-item.breadcrumb');
        breadcrumbs.forEach(bc => {
            const currentSlot = parseInt(bc.dataset.slotIndex || '0');
            const newSlot = currentSlot + 1;
            bc.dataset.slotIndex = String(newSlot);
            this._applyBreadcrumbSlot(bc, newSlot, true);
        });
    }

    /** Shift all existing breadcrumbs one slot to the right (shallower) */
    _shiftBreadcrumbsRight() {
        const breadcrumbs = this.container.querySelectorAll('.arc-item.breadcrumb');
        breadcrumbs.forEach(bc => {
            const currentSlot = parseInt(bc.dataset.slotIndex || '0');
            const newSlot = Math.max(0, currentSlot - 1);
            bc.dataset.slotIndex = String(newSlot);
            this._applyBreadcrumbSlot(bc, newSlot, true);
        });
    }

    /** Apply visual properties for a breadcrumb slot position */
    _applyBreadcrumbSlot(element, slotIndex, animate = false) {
        const maxSlot = this.BREADCRUMB_SLOTS.length - 1;
        const slot = this.BREADCRUMB_SLOTS[Math.min(slotIndex, maxSlot)];

        if (animate) {
            element.style.transition = 'transform 250ms ease-out, opacity 250ms ease-out';
        }

        // For slots beyond the defined ones, collapse further
        let x = slot.x;
        let scale = slot.scale;
        if (slotIndex > maxSlot) {
            x = slot.x - (slotIndex - maxSlot) * 10;
            scale = Math.max(0.4, slot.scale - (slotIndex - maxSlot) * 0.05);
        }

        element.style.transform = `translate(${x}px, 0px) scale(${scale})`;
        element.style.opacity = slotIndex > maxSlot ? '0.3' : '1';

        // Show/hide name based on slot
        const nameEl = element.querySelector('.item-name');
        if (nameEl) {
            if (slot.showName) {
                nameEl.style.opacity = String(slot.nameOpacity);
                nameEl.style.fontSize = slot.nameSize;
                nameEl.style.display = '';
            } else {
                nameEl.style.display = 'none';
            }
        }

        // Adjust image size
        const imgEl = element.querySelector('.item-image');
        if (imgEl) {
            imgEl.style.width = `${slot.imageSize}px`;
            imgEl.style.height = `${slot.imageSize}px`;
        }
    }

    /** Create a static breadcrumb element (no animation from an existing element) */
    _createStaticBreadcrumb(item, slotIndex) {
        const bc = document.createElement('div');
        bc.className = 'arc-item breadcrumb';
        bc.dataset.breadcrumbDepth = String(this.depth - 1);
        bc.dataset.slotIndex = String(slotIndex);
        bc.dataset.itemId = item.id;
        bc.style.position = 'absolute';
        bc.style.top = '50%';
        bc.style.left = '50%';
        bc.style.marginLeft = '-140px';
        bc.style.marginTop = '-64px';
        bc.style.zIndex = '10';
        bc.style.pointerEvents = 'auto';

        const nameEl = document.createElement('div');
        nameEl.className = 'item-name';
        nameEl.textContent = item.name;

        const imgEl = this.createImageElement(item);

        bc.appendChild(nameEl);
        bc.appendChild(imgEl);
        this.container.appendChild(bc);

        this._applyBreadcrumbSlot(bc, slotIndex, false);
        return bc;
    }

    /** Recreate breadcrumb DOM from nav stack (used on restore) */
    _createBreadcrumbsFromStack() {
        for (let i = 0; i < this.navStack.length; i++) {
            const frame = this.navStack[i];
            const slotIndex = this.navStack.length - 1 - i; // most recent = slot 0
            const bc = this._createStaticBreadcrumb(frame.selectedItem, slotIndex);
            frame.breadcrumbElement = bc;
        }
    }

    // ─── BUTTON ROUTING ──────────────────────────────────────────────

    handleButton(button) {
        // Hold-GO context menu control messages (from the parent page).
        if (button === 'context_open') { this.openContextMenu(); return; }
        if (button === 'go_release') { this.executeContextRelease(); return; }

        // While the menu owns the arc, the nav wheel scrolls the actions
        // (handleNavFromParent, unchanged); › closes it, ‹ and a plain GO are
        // ignored (release is what executes an action).
        if (this._contextMenuOpen) {
            if (button === 'right') this.closeContextMenu();
            return;
        }

        if (button === 'left') {
            if (this._inPageView) return;
            if (this.canDrillDown()) {
                this.drillDown();
            } else {
                this.sendButtonWebhook('left');
            }
        } else if (button === 'right') {
            if (this.depth > 0) {
                this.goBack();
            } else {
                this.sendButtonWebhook('right');
            }
        } else if (button === 'go') {
            if (this._inPageView) return;
            this.handleGo();
        }
    }

    /** Check if an item is actionable (GO does something).
     *  Explicit `actionable` field wins; otherwise defaults to leaf. */
    isActionable(item) {
        if (item.actionable !== undefined) return !!item.actionable;
        // Pages are navigatable (drill-in), not actionable
        if (this.isPage(item)) return false;
        // Default: leaves are actionable, containers are not
        const level = this.getLevelDescriptor(this.depth);
        return !level.isContainer || !level.isContainer(item);
    }

    handleGo() {
        this.snapToNearest();
        const idx = Math.round(this.currentIndex);
        const item = this.items[idx];
        if (!item) return;

        // Custom onGo callback — let the callback decide what's actionable
        if (this.config.onGo) {
            const el = this.container.querySelector('.arc-item.selected');
            if (el && item.actionable !== false) {
                el.classList.add('go-flash');
                setTimeout(() => el.classList.remove('go-flash'), 400);
            }
            const pathContext = this.navStack.map(f => f.selectedItem);
            this.config.onGo(item, this.depth, pathContext, idx);
            return;
        }

        // Default: only leaves are actionable
        if (!this.isActionable(item)) return;

        // Blue flash on the selected item
        const el = this.container.querySelector('.arc-item.selected');
        if (el) {
            el.classList.add('go-flash');
            setTimeout(() => el.classList.remove('go-flash'), 400);
        }

        // Default: send webhook
        this._sendGoWebhook(item, idx);
    }

    // ─── EVENT LISTENERS ─────────────────────────────────────────────

    setupEventListeners() {
        document.addEventListener('keydown', (e) => this.handleKeyPress(e));
        this.setupSnapTimer();

        // WebSocket
        this.connectWebSocket();

        // Periodic save + unload save
        this._saveInterval = setInterval(() => this.saveState(), 1000);
        window.addEventListener('beforeunload', () => this.saveState());

        // postMessage from parent iframe
        this._messageHandler = (event) => {
            if (event.data?.type === 'button') {
                this.handleButton(event.data.button || event.data.data?.button);
            } else if (event.data?.type === 'nav') {
                this.handleNavFromParent(event.data.data);
            } else if (event.data?.type === 'keyboard') {
                this.handleKeyPress({
                    key: event.data.key,
                    code: event.data.code,
                    preventDefault: () => {},
                    stopPropagation: () => {},
                });
            } else if (event.data?.type === 'reload-data') {
                this.reloadData();
            }
        };
        window.addEventListener('message', this._messageHandler);
    }

    handleKeyPress(e) {
        if (this._inPageView && this._pageScrollEl) {
            if (e.key === 'ArrowUp') {
                this._pageScrollEl.scrollTop -= 40;
                return;
            }
            if (e.key === 'ArrowDown') {
                this._pageScrollEl.scrollTop += 40;
                return;
            }
        }

        this.lastScrollTime = Date.now();

        if (e.key === 'ArrowUp') {
            this.targetIndex = Math.max(0, this.targetIndex - this.SCROLL_STEP);
            this.setupSnapTimer();
        } else if (e.key === 'ArrowDown') {
            this.targetIndex = Math.min(this.items.length - 1, this.targetIndex + this.SCROLL_STEP);
            this.setupSnapTimer();
        } else if (e.key === 'ArrowLeft') {
            this.handleButton('left');
        } else if (e.key === 'ArrowRight') {
            this.handleButton('right');
        } else if (e.key === 'Enter') {
            this.handleButton('go');
        }
    }

    handleNavFromParent(data) {
        if (!data) return;
        if (this._inPageView && this._pageScrollEl) {
            const direction = data.direction;
            const speed = data.speed || 1;
            if (direction === 'clock') {
                this._pageScrollEl.scrollTop += speed * 8;
            } else if (direction === 'counter') {
                this._pageScrollEl.scrollTop -= speed * 8;
            }
            return;
        }
        const direction = data.direction;
        const speed = data.speed || 1;
        const speedMultiplier = Math.min(speed / 10, 5);
        const scrollStep = this.SCROLL_STEP * speedMultiplier;

        const atTop = this.targetIndex <= 0;
        const atBottom = this.targetIndex >= this.items.length - 1;

        if (direction === 'counter' && !atTop) {
            this.targetIndex = Math.max(0, this.targetIndex - scrollStep);
            this.setupSnapTimer();
        } else if (direction === 'clock' && !atBottom) {
            this.targetIndex = Math.min(this.items.length - 1, this.targetIndex + scrollStep);
            this.setupSnapTimer();
        }

        if (!this.animationFrame) {
            this.currentIndex = this.targetIndex;
        }
    }

    // ─── SNAP TIMER ──────────────────────────────────────────────────

    setupSnapTimer() {
        if (this.snapTimer) clearTimeout(this.snapTimer);
        this.snapTimer = setTimeout(() => {
            if (Date.now() - this.lastScrollTime >= this.SNAP_DELAY) {
                const closest = Math.round(this.targetIndex);
                this.targetIndex = Math.max(0, Math.min(this.items.length - 1, closest));
            }
        }, this.SNAP_DELAY);
    }

    snapToNearest() {
        const nearest = Math.round(this.currentIndex);
        const clamped = Math.max(0, Math.min(this.items.length - 1, nearest));
        this.currentIndex = clamped;
        this.targetIndex = clamped;
        if (this.snapTimer) {
            clearTimeout(this.snapTimer);
            this.snapTimer = null;
        }
    }

    // ─── ANIMATION LOOP ─────────────────────────────────────────────

    startAnimation() {
        let lastRenderedIndex = this.currentIndex;
        let lastRenderTime = 0;
        const MIN_RENDER_INTERVAL = 16;

        const animate = () => {
            const diff = this.targetIndex - this.currentIndex;
            const previousIndex = this.currentIndex;

            if (Math.abs(diff) < 0.01) {
                this.currentIndex = this.targetIndex;
            } else {
                this.currentIndex += diff * this.SCROLL_SPEED;
            }

            this.checkForSelectionClick();

            const positionChanged = Math.abs(this.currentIndex - lastRenderedIndex) > 0.001;
            const now = Date.now();
            const enoughTimeElapsed = (now - lastRenderTime) >= MIN_RENDER_INTERVAL;

            if (positionChanged && enoughTimeElapsed && !this.isAnimating) {
                this.render();
                lastRenderedIndex = this.currentIndex;
                lastRenderTime = now;
            }

            if (previousIndex !== this.currentIndex) {
                this.updateCounter();
            }

            this.animationFrame = requestAnimationFrame(animate);
        };
        animate();
    }

    _pauseAnimation() {
        if (this.snapTimer) {
            clearTimeout(this.snapTimer);
            this.snapTimer = null;
        }
        if (this.animationFrame) {
            cancelAnimationFrame(this.animationFrame);
            this.animationFrame = null;
        }
    }

    _resumeAnimation() {
        this.startAnimation();
        this.setupSnapTimer();
    }

    // ─── RENDERING ───────────────────────────────────────────────────

    getVisibleItems() {
        const items = ArcMath.getVisibleItems(this.currentIndex, this.items, {
            middleIndex:          this.MIDDLE_INDEX,
            baseXOffset:          this.BASE_X_OFFSET,
            maxRadius:            this.MAX_RADIUS,
            horizontalMultiplier: this.HORIZONTAL_MULTIPLIER,
            baseItemSize:         this.BASE_ITEM_SIZE,
        });
        items.sort((a, b) => a.relativePosition - b.relativePosition);
        return items;
    }

    render() {
        if (this.isAnimating) return;
        if (this._inPageView) return;

        // Try to update existing elements in-place (fast path)
        if (this._updateExistingElements()) return;

        const visibleItems = this.getVisibleItems();

        // Reconcile by item id instead of rebuilding: a wheel scroll shifts
        // the visible window one slot per frame, so most rows survive from
        // the previous render — move their DOM nodes and skip their content.
        // The old tear-down-and-recreate path made a fresh <img> per row per
        // frame (decode + GPU texture upload); on the Pi a fast scroll
        // exhausted Chromium's GPU command buffer and crashed the renderer.
        // dataset.sig guards the reuse: fallback ids (`item-${index}`) can
        // collide across levels, so content is rebuilt whenever it differs.
        const existing = new Map();
        Array.from(this.container.querySelectorAll('.arc-item:not(.breadcrumb)'))
            .forEach(el => existing.set(el.dataset.itemId, el));
        const used = new Set();

        // Profiling on-device (a sustained fast flick, ~60 render() calls)
        // showed the DOM writes below — not row creation itself — as the
        // dominant per-frame cost: every one of the ~9 visible rows got an
        // unconditional className rewrite, an !important opacity/filter
        // write, and an appendChild reorder EVERY frame, even though those
        // values are almost always already correct (selected/actionable
        // status rarely flips frame-to-frame, opacity/filter are pinned to
        // '1'/'none' once settled, and the sorted-by-relativePosition order
        // is stable while the window slides ~1 item/frame). Each of those is
        // a style-recalc/paint-order trigger regardless of whether the value
        // actually changed, so skipping the no-op writes below is what
        // removed the stutter — row/image creation is unavoidable and cheap
        // by comparison (see 8bdcfbf's velocity gate for the actual image
        // decode cost).
        let prevEl = null;

        visibleItems.forEach(item => {
            const isSelected = Math.abs(item.index - this.currentIndex) < 0.5;
            // Container/page detection (stack effect for drill-down-able items)
            const navigatable = this.isContainer(item) || this.isPage(item);
            const sig = `${item.name}|${item.image || ''}|${item.icon || ''}|${navigatable}`;

            let el = existing.get(item.id);
            if (el && used.has(el)) el = null;   // duplicate id in this frame
            const isNew = !el;
            if (!el) {
                el = document.createElement('div');
                el.dataset.itemId = item.id;
            }
            used.add(el);

            let className = 'arc-item';
            if (isSelected) className += ' selected';
            // Section header row (e.g. Discover list titles) — styled distinctly
            if (item.header) className += ' arc-header';
            // Row with a small cover left of the text (Discover items)
            if (item.cover) className += ' arc-cover';
            // Actionable detection (blue highlight on GO-able items)
            if (this.isActionable(item)) className += ' actionable';
            if (navigatable) className += ' navigatable';
            if (el.className !== className) el.className = className;

            if (el.dataset.sig !== sig) {
                el.dataset.sig = sig;
                el.textContent = '';
                const nameEl = document.createElement('div');
                nameEl.textContent = item.name;
                el.appendChild(nameEl);
                el.appendChild(this.createImageWrapper(item, navigatable));
            }
            const nameEl = el.firstChild;
            const nameClass = `item-name ${isSelected ? 'selected' : 'unselected'}`;
            if (nameEl.className !== nameClass) nameEl.className = nameClass;

            const transform = `translate(${item.x}px, ${item.y}px) scale(${item.scale})`;
            if (el.style.transform !== transform) el.style.transform = transform;
            if (el.style.opacity !== '1') el.style.setProperty('opacity', '1', 'important');
            if (el.style.filter !== 'none') el.style.setProperty('filter', 'none', 'important');

            // Only move the node in the DOM if it isn't already right after
            // the previous item — insertBefore/appendChild is a paint-order
            // mutation even when the element ends up in the same place, so
            // skip it for the common case (order unchanged between frames).
            // The pairwise chaining guarantees the surviving rows keep the
            // correct relative (sorted) order among themselves; where the
            // first row sits relative to breadcrumbs doesn't matter — those
            // have an explicit z-index and always paint above regular rows.
            if (prevEl) {
                if (prevEl.nextSibling !== el) this.container.insertBefore(el, prevEl.nextSibling);
            } else if (isNew) {
                this.container.appendChild(el);
            }
            prevEl = el;
        });

        existing.forEach(el => { if (!used.has(el)) el.remove(); });

        this.updateCounter();
    }

    _updateExistingElements() {
        const existing = Array.from(this.container.querySelectorAll('.arc-item:not(.breadcrumb)'));
        const visibleItems = this.getVisibleItems();

        if (existing.length !== visibleItems.length) return false;

        for (let i = 0; i < existing.length; i++) {
            if (existing[i].dataset.itemId !== visibleItems[i]?.id) return false;
        }

        existing.forEach((el, i) => {
            const item = visibleItems[i];
            if (!item) return;

            el.classList.remove('playlist-enter', 'track-exit');
            const transform = `translate(${item.x}px, ${item.y}px) scale(${item.scale})`;
            if (el.style.transform !== transform) el.style.transform = transform;
            if (el.style.opacity !== '1') el.style.setProperty('opacity', '1', 'important');
            if (el.style.filter !== 'none') el.style.filter = 'none';

            const isSelected = Math.abs(item.index - this.currentIndex) < 0.5;
            const nameEl = el.firstChild;

            if (isSelected && !el.classList.contains('selected')) {
                el.classList.add('selected');
                if (nameEl) { nameEl.classList.add('selected'); nameEl.classList.remove('unselected'); }
            } else if (!isSelected && el.classList.contains('selected')) {
                el.classList.remove('selected');
                if (nameEl) { nameEl.classList.remove('selected'); nameEl.classList.add('unselected'); }
            }
        });

        return true;
    }

    /** Wrap image element in a container for stack effect + selection glow */
    createImageWrapper(item, navigatable) {
        const wrapper = document.createElement('div');
        wrapper.className = 'item-image-wrap';
        if (navigatable) wrapper.classList.add('stack');
        wrapper.appendChild(this.createImageElement(item));
        return wrapper;
    }

    createImageElement(item) {
        // Phosphor icon mode
        if (item.icon) {
            const iconDiv = document.createElement('div');
            iconDiv.className = 'item-image item-icon';
            const i = document.createElement('i');
            i.className = `ph ph-${item.icon}`;
            if (item.color) i.style.color = item.color;
            iconDiv.appendChild(i);
            iconDiv.dataset.itemId = item.id;
            return iconDiv;
        }

        // Image mode
        const img = document.createElement('img');
        img.className = 'item-image';
        img.alt = item.name;
        img.loading = 'lazy';
        img.decoding = 'async';
        img.dataset.itemId = item.id;

        img.onload = () => img.removeAttribute('data-loading');
        img.onerror = () => {
            const fallbackColor = '4A90E2';
            const fallbackText = (item.name || '??').substring(0, 2).toUpperCase();
            img.src = `data:image/svg+xml,%3Csvg width='128' height='128' xmlns='http://www.w3.org/2000/svg'%3E%3Crect width='128' height='128' fill='%23${fallbackColor}'/%3E%3Ctext x='64' y='64' text-anchor='middle' dy='.3em' fill='white' font-size='20' font-family='Arial, sans-serif'%3E${fallbackText}%3C/text%3E%3C/svg%3E`;
        };

        img.setAttribute('data-loading', 'true');
        if (!item.image) {
            img.src = ARC_PLACEHOLDER_SRC;
        } else if (this._isFastScrolling()) {
            // Velocity gate: during a fast wheel spin every animation frame
            // brings new rows into the window — loading each row's artwork
            // immediately meant a decode + GPU texture upload per row per
            // frame, which exhausted the GPU process's transfer buffers on
            // the Pi and killed the renderer. Defer: placeholder now, real
            // src when the wheel settles (_scheduleImageSettle).
            img.src = ARC_PLACEHOLDER_SRC;
            img.dataset.src = item.image;
            this._scheduleImageSettle();
        } else {
            img.src = item.image;
        }
        return img;
    }

    /** True while the wheel is actively spinning: recent scroll input and
     *  the eased index still far from its target. */
    _isFastScrolling() {
        return (Date.now() - this.lastScrollTime) < 250
            && Math.abs(this.targetIndex - this.currentIndex) > 1.5;
    }

    /** (Re)arm the settle timer; when it fires with the wheel at rest, load
     *  the deferred artwork of the rows still in the window. Rows that left
     *  the window were removed from the DOM — their images never load. */
    _scheduleImageSettle() {
        if (this._imgSettleTimer) clearTimeout(this._imgSettleTimer);
        this._imgSettleTimer = setTimeout(() => {
            this._imgSettleTimer = null;
            if (this._isFastScrolling()) {       // still spinning — wait more
                this._scheduleImageSettle();
                return;
            }
            this.container.querySelectorAll('img[data-src]').forEach(img => {
                img.src = img.dataset.src;
                delete img.dataset.src;
            });
        }, 250);
    }

    // ─── COUNTER + PATH ──────────────────────────────────────────────

    updateCounter() {
        if (this._inPageView) {
            this.currentItemDisplay.textContent = '';
            this.totalItemsDisplay.textContent = '';
        } else {
            const displayIndex = Math.floor(this.currentIndex) + 1;
            this.currentItemDisplay.textContent = displayIndex;
            this.totalItemsDisplay.textContent = this.items.length;
        }

        // Update path indicator
        if (this.counterPath) {
            if (this.depth >= 2 && this.navStack.length > 0) {
                const pathStr = this.navStack.map(f => f.selectedItem.name).join(' > ');
                this.counterPath.textContent = pathStr;
                this.counterPath.style.display = '';
            } else if (this.depth === 1 && this.navStack.length > 0) {
                // At depth 1, show parent name above counter
                this.counterPath.textContent = this.navStack[0].selectedItem.name;
                this.counterPath.style.display = '';
            } else {
                this.counterPath.textContent = '';
                this.counterPath.style.display = 'none';
            }
        }
    }

    // ─── WEBHOOKS ────────────────────────────────────────────────────

    getDeviceName() {
        try { return window.parent?.AppConfig?.deviceName || 'unknown'; }
        catch (e) { return 'unknown'; }
    }

    async sendButtonWebhook(button) {
        const webhookData = {
            device_type: 'Panel',
            device_name: this.getDeviceName(),
            panel_context: this.config.context,
            button: button,
            id: '1',
            depth: this.depth,
        };

        try {
            await fetch(this.config.webhookUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(webhookData),
            });
        } catch (e) {
            console.error(`Webhook error (${button}):`, e.message);
        }
    }

    async _sendGoWebhook(item, idx) {
        const path = this.navStack.map(f => f.selectedItem.id);
        path.push(item.id);

        let id = item.id;
        let parentId = null;

        // Spotify context: prepend spotify URI
        if (this.config.context === 'spotify') {
            if (this.depth === 0) {
                id = `spotify:playlist:${item.id}`;
            } else if (this.depth === 1) {
                id = `spotify:track:${item.id}`;
                parentId = `spotify:playlist:${this.navStack[0]?.selectedItem?.id}`;
            }
        }

        const webhookData = {
            device_type: 'Panel',
            device_name: this.getDeviceName(),
            panel_context: this.config.context,
            button: 'go',
            id: id,
            path: path,
            depth: this.depth,
        };
        if (parentId) webhookData.parent_id = parentId;

        try {
            await fetch(this.config.webhookUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(webhookData),
            });
        } catch (e) {
            console.error('Webhook error (go):', e.message);
        }

        // Emulator bridge
        this._notifyEmulator(id, item.name);
    }

    _notifyEmulator(id, itemName) {
        if (!window.EmulatorBridge?.isInEmulator) return;
        if (this.config.context === 'scenes') {
            window.EmulatorBridge.notifySceneActivated(id, itemName);
        } else if (this.config.context === 'spotify') {
            if (this.depth === 0) {
                window.EmulatorBridge.notifyPlaylistSelected(id, itemName);
            } else {
                const parent = this.navStack[0]?.selectedItem;
                window.EmulatorBridge.notifyTrackSelected(
                    Math.round(this.currentIndex), itemName,
                    parent?.id, parent?.name
                );
            }
        }
    }

    // ─── WEBSOCKET ───────────────────────────────────────────────────

    connectWebSocket() {
        if (window.parent !== window) return;

        try {
            this.ws = new WebSocket(this.config.webSocketUrl);

            const timeout = setTimeout(() => {
                if (this.ws?.readyState === WebSocket.CONNECTING) {
                    this.ws.close();
                    this.ws = null;
                }
            }, 2000);

            this.ws.onopen = () => {
                clearTimeout(timeout);
                console.log('WebSocket connected');
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    this._handleWebSocketMessage(data);
                } catch (e) { /* ignore parse errors */ }
            };

            this.ws.onclose = () => {
                clearTimeout(timeout);
                const wasConnected = this.ws !== null;
                this.ws = null;
                if (wasConnected) setTimeout(() => this.connectWebSocket(), 5000);
            };

            this.ws.onerror = () => {
                clearTimeout(timeout);
                this.ws = null;
            };
        } catch (e) {
            this.ws = null;
        }
    }

    _handleWebSocketMessage(data) {
        if (data.type === 'button' && data.data?.button) {
            this.handleButton(data.data.button);
            return;
        }

        if (data.type === 'nav' && data.data) {
            this.handleNavFromParent(data.data);
        }
    }

    // ─── SELECTION CLICK ─────────────────────────────────────────────

    checkForSelectionClick() {
        const centerIndex = Math.round(this.currentIndex);
        const currentItem = this.items[centerIndex];
        if (currentItem && currentItem.id !== this.lastClickedItemId) {
            this.sendClickCommand();
            this.lastClickedItemId = currentItem.id;
        }
    }

    /** Send click command back to server (rate-limited, 50ms throttle) */
    sendClickCommand() {
        try {
            const now = Date.now();
            if (now - (this.lastClickTime || 0) < 50) return;
            this.lastClickTime = now;

            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ type: 'command', command: 'click', params: {} }));
            } else if (window.parent !== window) {
                window.parent.postMessage({ type: 'click' }, '*');
            }
        } catch (e) {
            // Silently fail
        }
    }

    // ─── UTILITIES ───────────────────────────────────────────────────

    delay(ms) {
        if (!this._animationAbort) return new Promise(resolve => setTimeout(resolve, ms));
        const signal = this._animationAbort.signal;
        return new Promise((resolve) => {
            if (signal.aborted) { resolve(); return; }
            const timer = setTimeout(resolve, ms);
            signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
        });
    }

    /** Abort any in-progress animation, skip to final state */
    _abortAnimation() {
        if (!this.isAnimating) return;
        // Signal all pending delays to resolve immediately
        if (this._animationAbort) this._animationAbort.abort();
        // Kill all CSS transitions instantly
        this.container.querySelectorAll('.arc-item').forEach(el => {
            el.style.transition = 'none';
            el.style.transitionDelay = '';
        });
        // Clean up: remove transitional elements, keep breadcrumbs for stack frames
        this.container.querySelectorAll('.arc-item.parent-hidden, .arc-item.track-exit').forEach(el => el.remove());
        const pageEl = this.container.querySelector('.page-view');
        if (pageEl) pageEl.remove();
        this.isAnimating = false;
    }

    destroy() {
        if (this.animationFrame) {
            cancelAnimationFrame(this.animationFrame);
            this.animationFrame = null;
        }
        if (this.snapTimer) {
            clearTimeout(this.snapTimer);
            this.snapTimer = null;
        }
        if (this._imgSettleTimer) {
            clearTimeout(this._imgSettleTimer);
            this._imgSettleTimer = null;
        }
        if (this._saveInterval) {
            clearInterval(this._saveInterval);
            this._saveInterval = null;
        }
        if (this._messageHandler) {
            window.removeEventListener('message', this._messageHandler);
            this._messageHandler = null;
        }
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    revive() {
        if (!this.animationFrame) this.startAnimation();
        if (!this._saveInterval) {
            this._saveInterval = setInterval(() => this.saveState(), 1000);
        }
        if (!this._messageHandler) {
            this._messageHandler = (event) => {
                if (event.data?.type === 'button') {
                    this.handleButton(event.data.button || event.data.data?.button);
                } else if (event.data?.type === 'nav') {
                    this.handleNavFromParent(event.data.data);
                } else if (event.data?.type === 'keyboard') {
                    this.handleKeyPress({
                        key: event.data.key,
                        code: event.data.code,
                        preventDefault: () => {},
                        stopPropagation: () => {},
                    });
                } else if (event.data?.type === 'reload-data') {
                    this.reloadData();
                }
            };
            window.addEventListener('message', this._messageHandler);
        }
        this.render();
    }
}

// No automatic initialization — each HTML page controls its own setup
