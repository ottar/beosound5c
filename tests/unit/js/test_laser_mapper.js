/**
 * Tests for web/js/laser-position-mapper.js
 *
 * Uses Node.js built-in test runner (node:test + node:assert).
 * Run with: node --test tests/unit/js/test_laser_mapper.js
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');

const {
    laserPositionToAngle,
    angleToLaserPosition,
    resolveMenuSelection,
    getMenuStartAngle,
    getMenuItemAngle,
    LASER_MAPPING_CONFIG
} = require('../../../web/js/laser-position-mapper.js');


// --- laserPositionToAngle ---

describe('laserPositionToAngle', () => {
    it('min position (3) maps to 150 degrees', () => {
        assert.equal(laserPositionToAngle(3), 150);
    });

    it('mid position (72) maps to 180 degrees', () => {
        assert.equal(laserPositionToAngle(72), 180);
    });

    it('max position (123) maps to 210 degrees', () => {
        assert.equal(laserPositionToAngle(123), 210);
    });

    it('below min clamps to 150 degrees', () => {
        assert.equal(laserPositionToAngle(0), 150);
        assert.equal(laserPositionToAngle(-10), 150);
    });

    it('above max clamps to 210 degrees', () => {
        assert.equal(laserPositionToAngle(200), 210);
    });

    it('lower-half interpolation (between 150 and 180)', () => {
        const angle = laserPositionToAngle(37);  // roughly halfway between 3 and 72
        assert.ok(angle > 150, `expected > 150, got ${angle}`);
        assert.ok(angle < 180, `expected < 180, got ${angle}`);
    });

    it('upper-half interpolation (between 180 and 210)', () => {
        const angle = laserPositionToAngle(97);  // roughly halfway between 72 and 123
        assert.ok(angle > 180, `expected > 180, got ${angle}`);
        assert.ok(angle < 210, `expected < 210, got ${angle}`);
    });

    it('output is monotonically increasing', () => {
        let prev = laserPositionToAngle(3);
        for (let pos = 4; pos <= 123; pos++) {
            const curr = laserPositionToAngle(pos);
            assert.ok(curr >= prev, `angle decreased at position ${pos}: ${prev} -> ${curr}`);
            prev = curr;
        }
    });
});


// --- angleToLaserPosition (inverse) ---

describe('angleToLaserPosition', () => {
    it('150 degrees maps to position 3', () => {
        assert.equal(angleToLaserPosition(150), 3);
    });

    it('180 degrees maps to position 72', () => {
        assert.equal(angleToLaserPosition(180), 72);
    });

    it('210 degrees maps to position 123', () => {
        assert.equal(angleToLaserPosition(210), 123);
    });

    it('roundtrip: angleToLaserPosition(laserPositionToAngle(x)) ≈ x', () => {
        const testPositions = [3, 20, 37, 50, 72, 90, 100, 110, 123];
        for (const pos of testPositions) {
            const angle = laserPositionToAngle(pos);
            const roundtrip = angleToLaserPosition(angle);
            assert.ok(
                Math.abs(roundtrip - pos) < 0.01,
                `Position ${pos}: angle=${angle}, roundtrip=${roundtrip}`
            );
        }
    });

    it('clamps below 150 to position 3', () => {
        assert.equal(angleToLaserPosition(100), 3);
    });

    it('clamps above 210 to position 123', () => {
        assert.equal(angleToLaserPosition(250), 123);
    });
});


// --- resolveMenuSelection ---

describe('resolveMenuSelection', () => {
    it('top overlay position (angle <= 160) returns isOverlay: true', () => {
        // Position 3 = 150 degrees, well within top overlay
        const result = resolveMenuSelection(3);
        assert.equal(result.isOverlay, true);
        assert.equal(result.selectedIndex, -1);
        assert.equal(result.path, null);
    });

    it('bottom overlay position (angle >= 200) returns isOverlay: true', () => {
        // Position 123 = 210 degrees, well within bottom overlay
        const result = resolveMenuSelection(123);
        assert.equal(result.isOverlay, true);
        assert.equal(result.selectedIndex, -1);
        assert.equal(result.path, null);
    });

    it('middle position selects a menu item', () => {
        // Find a position that maps to 180 degrees (center of menu)
        const result = resolveMenuSelection(72);
        // 180 degrees is the center — should hit a menu item
        assert.equal(result.isOverlay, false);
        assert.ok(result.selectedIndex >= 0, `Expected a selected item, got index ${result.selectedIndex}`);
        assert.ok(result.path !== null, 'Expected a non-null path');
    });

    it('gap between items returns selectedIndex -1, isOverlay false', () => {
        // Test many positions — at least some should fall in gaps
        let foundGap = false;
        for (let pos = 3; pos <= 123; pos++) {
            const result = resolveMenuSelection(pos);
            if (!result.isOverlay && result.selectedIndex === -1) {
                foundGap = true;
                assert.equal(result.path, null);
                break;
            }
        }
        // With 5 menu items at 5-degree steps and 60-degree usable range,
        // gaps are expected
        assert.ok(foundGap, 'Expected to find at least one gap position');
    });

    it('selected items have valid paths from MENU_ITEMS', () => {
        const validPaths = LASER_MAPPING_CONFIG.MENU_ITEMS.map(i => i.path);
        for (let pos = 3; pos <= 123; pos++) {
            const result = resolveMenuSelection(pos);
            if (result.selectedIndex >= 0) {
                assert.ok(
                    validPaths.includes(result.path),
                    `Path '${result.path}' not in MENU_ITEMS`
                );
            }
        }
    });

    it('result always includes the computed angle', () => {
        const result = resolveMenuSelection(50);
        assert.equal(typeof result.angle, 'number');
        assert.ok(result.angle >= 150 && result.angle <= 210);
    });
});


// --- getMenuStartAngle + getMenuItemAngle ---

describe('menu angle geometry', () => {
    it('top item is pinned just below the top overlay boundary', () => {
        // Top-anchored: the top-most item (highest index) sits at
        // TOP_OVERLAY_START + step/2, independent of item count.
        const { TOP_OVERLAY_START, MENU_ANGLE_STEP, MENU_ITEMS } = LASER_MAPPING_CONFIG;
        const anchor = TOP_OVERLAY_START + MENU_ANGLE_STEP / 2;
        assert.equal(getMenuStartAngle(), anchor);
        assert.equal(getMenuItemAngle(MENU_ITEMS.length - 1), anchor);
    });

    it('item angles are monotonically decreasing with increasing index', () => {
        const items = LASER_MAPPING_CONFIG.MENU_ITEMS;
        for (let i = 1; i < items.length; i++) {
            const prev = getMenuItemAngle(i - 1);
            const curr = getMenuItemAngle(i);
            assert.ok(curr < prev, `Angle at index ${i} (${curr}) should be less than at ${i-1} (${prev})`);
        }
    });

    it('items grow downward from the pinned top anchor', () => {
        // Bottom-most item (index 0) sits step*(count-1) below the top.
        const { TOP_OVERLAY_START, MENU_ANGLE_STEP, MENU_ITEMS } = LASER_MAPPING_CONFIG;
        const anchor = TOP_OVERLAY_START + MENU_ANGLE_STEP / 2;
        const bottom = getMenuItemAngle(0);
        assert.equal(bottom, anchor + MENU_ANGLE_STEP * (MENU_ITEMS.length - 1));
        assert.ok(bottom > getMenuItemAngle(MENU_ITEMS.length - 1));
    });

    it('consecutive items are exactly MENU_ANGLE_STEP apart', () => {
        const items = LASER_MAPPING_CONFIG.MENU_ITEMS;
        const step = LASER_MAPPING_CONFIG.MENU_ANGLE_STEP;
        for (let i = 1; i < items.length; i++) {
            const diff = getMenuItemAngle(i - 1) - getMenuItemAngle(i);
            assert.ok(
                Math.abs(diff - step) < 0.01,
                `Step between ${i-1} and ${i} is ${diff}, expected ${step}`
            );
        }
    });
});
