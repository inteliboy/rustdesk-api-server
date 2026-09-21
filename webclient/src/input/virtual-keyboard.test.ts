import { describe, expect, it } from 'vitest';
import { ControlKey } from '../gen/message';
import type { UiCommand } from '../core/contracts';
import { MouseType, buttonMask, MouseButton } from './mouse-keyboard';
import {
  LOCK_TAP_MS,
  MAIN_LAYOUT,
  ModifierLatches,
  NAV_LAYOUT,
  SHORTCUTS,
  commandsForKey,
  consumesOneShot,
  isGap,
  type KeyAction,
  type Slot,
} from './virtual-keyboard';

const rowWidths = (slots: Slot[], columns: number): number[] => {
  const rows: number[] = [];
  let used = 0;
  for (const s of slots) {
    used += isGap(s) ? s.gap : s.w;
    if (used === columns) {
      rows.push(used);
      used = 0;
    }
    expect(used).toBeLessThan(columns);
  }
  expect(used).toBe(0);
  return rows;
};

describe('layout', () => {
  it('fills six rows of 60 columns in the main block and 12 in the nav block', () => {
    expect(rowWidths(MAIN_LAYOUT, 60)).toHaveLength(6);
    expect(rowWidths(NAV_LAYOUT, 12)).toHaveLength(6);
  });

  it('gives every key a unique id', () => {
    const ids = [...MAIN_LAYOUT, ...NAV_LAYOUT].filter((s) => !isGap(s)).map((s) => (isGap(s) ? '' : s.id));
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('ModifierLatches', () => {
  it('tap arms a one-shot, a quick second tap locks, a third clears', () => {
    const l = new ModifierLatches();
    expect(l.tap('ctrl', 1000)).toBe('once');
    expect(l.tap('ctrl', 1000 + LOCK_TAP_MS - 1)).toBe('locked');
    expect(l.tap('ctrl', 5000)).toBe('off');
    expect(l.any()).toBe(false);
  });

  it('a slow second tap cancels the one-shot instead of locking', () => {
    const l = new ModifierLatches();
    l.tap('alt', 1000);
    expect(l.tap('alt', 1000 + LOCK_TAP_MS + 1)).toBe('off');
  });

  it('releaseOnce keeps locked modifiers and drops one-shot ones', () => {
    const l = new ModifierLatches();
    l.tap('ctrl', 0);
    l.tap('ctrl', 10); // locked
    l.tap('shift', 20); // once
    l.releaseOnce();
    expect(l.active()).toEqual(['ctrl']);
    expect(l.keys()).toEqual([ControlKey.Control]);
  });

  it('reports changes, and clear() only when there was something to clear', () => {
    const l = new ModifierLatches();
    let n = 0;
    l.onChange = () => n++;
    l.clear();
    expect(n).toBe(0);
    l.tap('meta', 0);
    l.releaseOnce();
    l.clear();
    expect(n).toBe(2);
  });

  it('lists the active modifiers in a fixed order', () => {
    const l = new ModifierLatches();
    l.tap('meta', 0);
    l.tap('alt', 0);
    l.tap('ctrl', 0);
    expect(l.active()).toEqual(['ctrl', 'alt', 'meta']);
  });
});

describe('consumesOneShot', () => {
  const key = (down: boolean, press: boolean, value: number, keyKind: 'chr' | 'control' = 'control'): UiCommand => ({
    c: 'key', down, press, keyKind, value, modifiers: [],
  });

  it('is used up by a key going down, not by its release or by a modifier key', () => {
    expect(consumesOneShot(key(false, true, ControlKey.Tab))).toBe(true);
    expect(consumesOneShot(key(true, false, 0x61, 'chr'))).toBe(true);
    expect(consumesOneShot(key(false, false, 0x61, 'chr'))).toBe(false);
    expect(consumesOneShot(key(true, false, ControlKey.Shift))).toBe(false);
    expect(consumesOneShot(key(true, false, ControlKey.RControl))).toBe(false);
  });

  it('is used up when a mouse button comes up, not before', () => {
    const mouse = (mask: number): UiCommand => ({ c: 'mouse', mask, x: 0, y: 0, modifiers: [] });
    expect(consumesOneShot(mouse(buttonMask(MouseType.DOWN, MouseButton.LEFT)))).toBe(false);
    expect(consumesOneShot(mouse(buttonMask(MouseType.UP, MouseButton.LEFT)))).toBe(true);
    expect(consumesOneShot(mouse(buttonMask(MouseType.MOVE, 0)))).toBe(false);
  });
});

describe('commandsForKey', () => {
  const a: KeyAction = { t: 'chr', ch: 'a', shifted: 'A' };
  const one = (act: KeyAction, mods: Parameters<typeof commandsForKey>[1]): UiCommand => {
    const cmds = commandsForKey(act, mods);
    expect(cmds).toHaveLength(1);
    return cmds[0]!;
  };

  it('sends a plain character as one press', () => {
    expect(one(a, [])).toEqual({ c: 'key', down: false, press: true, keyKind: 'chr', value: 0x61, modifiers: [] });
  });

  it('types the shifted character, with Shift, when Shift is on', () => {
    expect(one(a, ['shift'])).toMatchObject({ keyKind: 'chr', value: 0x41, modifiers: [ControlKey.Shift] });
    expect(one({ t: 'chr', ch: '1', shifted: '!' }, ['shift'])).toMatchObject({ value: 0x21 });
  });

  it('keeps the plain character under Ctrl, like the physical keyboard', () => {
    expect(one(a, ['ctrl'])).toMatchObject({ value: 0x61, modifiers: [ControlKey.Control] });
  });

  it('turns Ctrl+Alt on Delete into the peer own Ctrl+Alt+Del', () => {
    const del: KeyAction = { t: 'ctl', key: ControlKey.Delete };
    expect(commandsForKey(del, ['ctrl', 'alt'])).toEqual([{ c: 'ctrlAltDel' }]);
    expect(one(del, ['ctrl'])).toMatchObject({ keyKind: 'control', value: ControlKey.Delete });
    expect(one(del, ['ctrl', 'alt', 'shift'])).toMatchObject({ keyKind: 'control', value: ControlKey.Delete });
  });

  it('sends nothing for a modifier key: those only latch', () => {
    expect(commandsForKey({ t: 'mod', mod: 'ctrl' }, [])).toEqual([]);
  });
});

describe('SHORTCUTS', () => {
  it('sends Ctrl+Alt+Del as the peer key and the rest as one press with their modifiers', () => {
    const by = (label: string) => SHORTCUTS.find((s) => s.label === label)!.run();
    expect(by('Ctrl+Alt+Del')).toEqual([{ c: 'ctrlAltDel' }]);
    expect(by('Alt+Tab')).toEqual([
      { c: 'key', down: false, press: true, keyKind: 'control', value: ControlKey.Tab, modifiers: [ControlKey.Alt] },
    ]);
    expect(by('Ctrl+Shift+Esc')[0]).toMatchObject({ value: ControlKey.Escape, modifiers: [ControlKey.Control, ControlKey.Shift] });
    expect(by('Win+D')[0]).toMatchObject({ keyKind: 'chr', value: 0x64, modifiers: [ControlKey.Meta] });
    expect(by('Lock')[0]).toMatchObject({ value: ControlKey.LockScreen, modifiers: [] });
  });
});

describe('AltGr and the Polish layout', () => {
  const key = (ch: string) => ({ t: 'chr', ch, shifted: ch.toUpperCase() }) as const;
  const cp = (s: string) => s.codePointAt(0);

  it('types the Polish letters as characters, with no modifiers', () => {
    const typed = [...'acelnosxz'].map((ch) => commandsForKey(key(ch), ['altgr'], 'pl')[0]);
    expect(typed.map((c) => (c && c.c === 'key' ? String.fromCodePoint(c.value) : ''))).toEqual([...'ąćęłńóśźż']);
    for (const c of typed) expect(c).toMatchObject({ keyKind: 'unicode', modifiers: [] });
    expect(commandsForKey(key('u'), ['altgr'], 'pl')[0]).toMatchObject({ keyKind: 'unicode', value: cp('€') });
  });

  it('with Shift they are capitals', () => {
    expect(commandsForKey(key('a'), ['shift', 'altgr'], 'pl')[0]).toMatchObject({ keyKind: 'unicode', value: cp('Ą'), modifiers: [] });
    expect(commandsForKey(key('z'), ['shift', 'altgr'], 'pl')[0]).toMatchObject({ value: cp('Ż') });
  });

  it('is Ctrl+Alt on a key with no national character, and a plain Alt in the US layout', () => {
    expect(commandsForKey(key('q'), ['altgr'], 'pl')[0]).toMatchObject({
      keyKind: 'chr', value: cp('q'), modifiers: [ControlKey.Control, ControlKey.Alt],
    });
    expect(commandsForKey(key('a'), ['altgr'], 'us')[0]).toMatchObject({ keyKind: 'chr', value: cp('a'), modifiers: [ControlKey.Alt] });
  });

  it('the plain keys are unchanged without AltGr', () => {
    expect(commandsForKey(key('a'), [], 'pl')[0]).toMatchObject({ keyKind: 'chr', value: cp('a'), modifiers: [] });
  });

  it('the latch stands for Ctrl+Alt in Polish (a click), an Alt in US, and is used up by one key', () => {
    const l = new ModifierLatches();
    l.layout = 'pl';
    l.tap('altgr', 0);
    expect(l.keys()).toEqual([ControlKey.Control, ControlKey.Alt]);
    l.layout = 'us';
    expect(l.keys()).toEqual([ControlKey.Alt]);
    l.releaseOnce();
    expect(l.state('altgr')).toBe('off');
  });

  it('the right Alt key is the AltGr latch; the left one stays Alt', () => {
    const all = MAIN_LAYOUT.filter((s) => 'act' in s);
    const right = all.find((s) => 'id' in s && s.id === 'alt-r');
    const left = all.find((s) => 'id' in s && s.id === 'alt-l');
    expect(right && 'act' in right ? right.act : null).toEqual({ t: 'mod', mod: 'altgr' });
    expect(left && 'act' in left ? left.act : null).toEqual({ t: 'mod', mod: 'alt' });
  });
});
