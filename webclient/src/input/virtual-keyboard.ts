// The on-screen keyboard's logic, apart from any DOM: the key layout, the Ctrl/Shift/Alt/Win latches and the
// commands a key or a shortcut turns into. The panel (ui/keyboard-panel.ts) only draws this and forwards taps.
import { ControlKey } from '../gen/message';
import type { UiCommand } from '../core/contracts';
import { MouseType } from './mouse-keyboard';

/** `altgr` is the right Alt key: what it does depends on the keyboard layout (see KeyboardLayout). */
export type Mod = 'ctrl' | 'shift' | 'alt' | 'altgr' | 'meta';

/** Which national characters AltGr types. The keys drawn are the same for every layout. */
export type KeyboardLayout = 'us' | 'pl';

// Polish (programmers): AltGr with a letter is that letter's Polish form, and AltGr+U is the euro sign.
const ALTGR_PL: Record<string, string> = {
  a: 'ą', c: 'ć', e: 'ę', l: 'ł', n: 'ń', o: 'ó', s: 'ś', x: 'ź', z: 'ż', u: '€',
};

/** The national character AltGr gives this key in this layout (Shift makes it a capital), or null. */
export function altGrChar(layout: KeyboardLayout, ch: string, shift: boolean): string | null {
  if (layout !== 'pl') return null;
  const nat = ALTGR_PL[ch.toLowerCase()];
  if (!nat) return null;
  return shift ? nat.toUpperCase() : nat;
}

// The protocol has no key for AltGr: on a Windows host it is Ctrl+Alt. In the US layout the right Alt is a plain Alt.
const MOD_KEYS: Record<Exclude<Mod, 'altgr'>, ControlKey> = {
  ctrl: ControlKey.Control,
  shift: ControlKey.Shift,
  alt: ControlKey.Alt,
  meta: ControlKey.Meta,
};
const MOD_ORDER: Mod[] = ['ctrl', 'shift', 'alt', 'altgr', 'meta'];

/** The protocol modifiers a latched modifier stands for in a layout. */
function protocolMods(mods: Mod[], layout: KeyboardLayout): Array<Exclude<Mod, 'altgr'>> {
  const out = new Set<Exclude<Mod, 'altgr'>>();
  for (const m of mods) {
    if (m !== 'altgr') out.add(m);
    else {
      out.add('alt');
      if (layout === 'pl') out.add('ctrl');
    }
  }
  return MOD_ORDER.filter((m): m is Exclude<Mod, 'altgr'> => m !== 'altgr' && out.has(m));
}

/** The control keys that are modifiers themselves: pressing one must not use up a one-shot latch. */
const MODIFIER_CONTROLS = new Set<number>([
  ControlKey.Control,
  ControlKey.Shift,
  ControlKey.Alt,
  ControlKey.Meta,
  ControlKey.RControl,
  ControlKey.RShift,
  ControlKey.RAlt,
  ControlKey.RWin,
]);

// --- latches -----------------------------------------------------------------------

/** off; once = until the next key or click has gone; locked = until tapped again. */
export type LatchState = 'off' | 'once' | 'locked';

/** A second tap this soon after the first turns a one-shot into a lock. */
export const LOCK_TAP_MS = 400;

/**
 * Ctrl, Shift, Alt and Win as a person taps them on the on-screen keyboard. They ride along in the modifiers
 * of the next key or click the app sends (a merge, never a synthetic key down/up), so a dropped session
 * cannot leave one stuck on the peer.
 */
export class ModifierLatches {
  private readonly states: Record<Mod, LatchState> = { ctrl: 'off', shift: 'off', alt: 'off', altgr: 'off', meta: 'off' };
  private readonly armedAt: Record<Mod, number> = { ctrl: 0, shift: 0, alt: 0, altgr: 0, meta: 0 };
  /** The layout the right Alt key follows. */
  layout: KeyboardLayout = 'us';
  /** Called after any change, so the keyboard can redraw. */
  onChange: (() => void) | undefined;

  state(mod: Mod): LatchState {
    return this.states[mod];
  }

  any(): boolean {
    return MOD_ORDER.some((m) => this.states[m] !== 'off');
  }

  /** The active modifiers, in a fixed order. */
  active(): Mod[] {
    return MOD_ORDER.filter((m) => this.states[m] !== 'off');
  }

  /** The active modifiers as the protocol's control keys. */
  keys(): ControlKey[] {
    return protocolMods(this.active(), this.layout).map((m) => MOD_KEYS[m]);
  }

  /** off -> once; once -> locked when tapped again quickly, else off; locked -> off. */
  tap(mod: Mod, now: number): LatchState {
    const before = this.states[mod];
    let after: LatchState = 'off';
    if (before === 'off') {
      after = 'once';
      this.armedAt[mod] = now;
    } else if (before === 'once' && now - this.armedAt[mod] <= LOCK_TAP_MS) {
      after = 'locked';
    }
    this.states[mod] = after;
    this.onChange?.();
    return after;
  }

  /** A key or a click went out: one-shot latches are used up, locked ones stay. */
  releaseOnce(): void {
    let changed = false;
    for (const m of MOD_ORDER) {
      if (this.states[m] === 'once') {
        this.states[m] = 'off';
        changed = true;
      }
    }
    if (changed) this.onChange?.();
  }

  clear(): void {
    if (!this.any()) return;
    for (const m of MOD_ORDER) this.states[m] = 'off';
    this.onChange?.();
  }
}

/** Does sending this command use up the one-shot latches? A key going down, or a mouse button coming up. */
export function consumesOneShot(cmd: UiCommand): boolean {
  if (cmd.c === 'key') {
    if (!cmd.down && !cmd.press) return false;
    return !(cmd.keyKind === 'control' && MODIFIER_CONTROLS.has(cmd.value));
  }
  if (cmd.c === 'mouse') return (cmd.mask & 7) === MouseType.UP;
  return false;
}

// --- keys ---------------------------------------------------------------------------

export type KeyAction =
  | { t: 'chr'; ch: string; shifted: string }
  | { t: 'ctl'; key: ControlKey }
  | { t: 'mod'; mod: Mod };

export type VKey = {
  id: string;
  /** What the keycap shows; the Space bar has none. */
  label: string;
  /** Width in quarters of a normal key. */
  w: number;
  act: KeyAction;
  /** Held down, it repeats like a real key. */
  repeat?: boolean;
  /** Read out for keys whose cap is a symbol. */
  aria?: string;
};
export type Gap = { gap: number };
export type Slot = VKey | Gap;
export const isGap = (s: Slot): s is Gap => 'gap' in s;

/** The US layout. The shifted character of each key is what it types, and shows, while Shift is on. */
const chr = (c: string, shifted: string, w = 4): VKey => ({
  id: `c-${c}`,
  label: c,
  w,
  act: { t: 'chr', ch: c, shifted },
  repeat: true,
});
const letter = (c: string): VKey => chr(c, c.toUpperCase());
const ctl = (id: string, label: string, key: ControlKey, w = 4, repeat = false, aria?: string): VKey => ({
  id,
  label,
  w,
  act: { t: 'ctl', key },
  repeat,
  aria,
});
const mod = (id: string, label: string, m: Mod, w: number): VKey => ({ id, label, w, act: { t: 'mod', mod: m } });
const gap = (n: number): Gap => ({ gap: n });

const F_KEYS: ControlKey[] = [
  ControlKey.F1, ControlKey.F2, ControlKey.F3, ControlKey.F4, ControlKey.F5, ControlKey.F6,
  ControlKey.F7, ControlKey.F8, ControlKey.F9, ControlKey.F10, ControlKey.F11, ControlKey.F12,
];
const fKey = (n: number): VKey => ctl(`f${n}`, `F${n}`, F_KEYS[n - 1]!);
const letters = (s: string): VKey[] => [...s].map(letter);

/** Six rows of 60 quarter-key columns. */
export const MAIN_LAYOUT: Slot[] = [
  ctl('esc', 'Esc', ControlKey.Escape),
  gap(4),
  fKey(1), fKey(2), fKey(3), fKey(4),
  gap(2),
  fKey(5), fKey(6), fKey(7), fKey(8),
  gap(2),
  fKey(9), fKey(10), fKey(11), fKey(12),

  chr('`', '~'), chr('1', '!'), chr('2', '@'), chr('3', '#'), chr('4', '$'), chr('5', '%'), chr('6', '^'),
  chr('7', '&'), chr('8', '*'), chr('9', '('), chr('0', ')'), chr('-', '_'), chr('=', '+'),
  ctl('backspace', '⌫', ControlKey.Backspace, 8, true, 'Backspace'),

  ctl('tab', 'Tab', ControlKey.Tab, 6, true),
  ...letters('qwertyuiop'), chr('[', '{'), chr(']', '}'), chr('\\', '|', 6),

  ctl('caps', 'Caps', ControlKey.CapsLock, 7),
  ...letters('asdfghjkl'), chr(';', ':'), chr("'", '"'),
  ctl('enter', 'Enter', ControlKey.Return, 9, true),

  mod('shift-l', 'Shift', 'shift', 9),
  ...letters('zxcvbnm'), chr(',', '<'), chr('.', '>'), chr('/', '?'),
  mod('shift-r', 'Shift', 'shift', 11),

  mod('ctrl-l', 'Ctrl', 'ctrl', 5),
  mod('meta-l', 'Win', 'meta', 5),
  mod('alt-l', 'Alt', 'alt', 5),
  ctl('space', '', ControlKey.Space, 25, true, 'Space'),
  mod('alt-r', 'Alt', 'altgr', 5),
  mod('meta-r', 'Win', 'meta', 5),
  ctl('menu', 'Menu', ControlKey.Apps, 5),
  mod('ctrl-r', 'Ctrl', 'ctrl', 5),
];

/** Six rows of 12 quarter-key columns, level with the main block's rows. */
export const NAV_LAYOUT: Slot[] = [
  ctl('prtsc', 'PrtSc', ControlKey.Snapshot), ctl('scroll', 'ScrLk', ControlKey.Scroll), ctl('pause', 'Pause', ControlKey.Pause),
  ctl('ins', 'Ins', ControlKey.Insert), ctl('home', 'Home', ControlKey.Home, 4, true),
  ctl('pgup', 'PgUp', ControlKey.PageUp, 4, true),
  ctl('del', 'Del', ControlKey.Delete, 4, true), ctl('end', 'End', ControlKey.End, 4, true),
  ctl('pgdn', 'PgDn', ControlKey.PageDown, 4, true),
  gap(12),
  gap(4), ctl('up', '↑', ControlKey.UpArrow, 4, true, 'Up'), gap(4),
  ctl('left', '←', ControlKey.LeftArrow, 4, true, 'Left'),
  ctl('down', '↓', ControlKey.DownArrow, 4, true, 'Down'),
  ctl('right', '→', ControlKey.RightArrow, 4, true, 'Right'),
];

// --- what a tap sends ---------------------------------------------------------------

const keyCmd = (
  keyKind: 'chr' | 'control' | 'unicode',
  value: number,
  modifiers: ControlKey[],
): UiCommand => ({ c: 'key', down: false, press: true, keyKind, value, modifiers });

/** True when Ctrl+Alt (and nothing else) sit on Delete: the peer has one key for that, and it is the only way in. */
const isCtrlAltDel = (act: KeyAction, mods: Array<Exclude<Mod, 'altgr'>>): boolean =>
  act.t === 'ctl' &&
  act.key === ControlKey.Delete &&
  mods.length === 2 &&
  mods.includes('ctrl') &&
  mods.includes('alt');

/**
 * The commands for a key with the given modifiers held. Shift picks the shifted character, like a real
 * keyboard (the physical-keyboard path also sends the character the key produced, plus Shift).
 */
export function commandsForKey(act: KeyAction, latched: Mod[], layout: KeyboardLayout = 'us'): UiCommand[] {
  if (act.t === 'mod') return [];
  // A national character is sent as that character, with no modifiers: the host's own layout may not have it
  // under AltGr, and Ctrl+Alt+A would be a shortcut there.
  if (act.t === 'chr' && latched.includes('altgr')) {
    const nat = altGrChar(layout, act.ch, latched.includes('shift'));
    if (nat) return [keyCmd('unicode', nat.codePointAt(0) ?? 0, [])];
  }
  const mods = protocolMods(latched, layout);
  if (isCtrlAltDel(act, mods)) return [{ c: 'ctrlAltDel' }];
  const modifiers = mods.map((m) => MOD_KEYS[m]);
  if (act.t === 'ctl') return [keyCmd('control', act.key, modifiers)];
  const ch = mods.includes('shift') ? act.shifted : act.ch;
  return [keyCmd('chr', ch.codePointAt(0) ?? 0, modifiers)];
}

export type Shortcut = { label: string; title: string; run: () => UiCommand[] };

const combo = (label: string, title: string, mods: Mod[], act: KeyAction): Shortcut => ({
  label,
  title,
  run: () => commandsForKey(act, mods),
});
const c = (ch: string): KeyAction => ({ t: 'chr', ch, shifted: ch.toUpperCase() });
const k = (key: ControlKey): KeyAction => ({ t: 'ctl', key });

/** One tap each. Ctrl+Alt+Del goes as the peer's own key (it cannot be typed on Windows). */
export const SHORTCUTS: Shortcut[] = [
  {
    label: 'Ctrl+Alt+Del',
    title: 'Secure attention: sign-in screen, Task Manager, lock',
    run: () => [{ c: 'ctrlAltDel' }],
  },
  combo('Ctrl+Shift+Esc', 'Task Manager', ['ctrl', 'shift'], k(ControlKey.Escape)),
  combo('Alt+Tab', 'Switch window', ['alt'], k(ControlKey.Tab)),
  combo('Alt+F4', 'Close the window', ['alt'], k(ControlKey.F4)),
  combo('Win+D', 'Show the desktop', ['meta'], c('d')),
  combo('Win+R', 'Run', ['meta'], c('r')),
  {
    label: 'Lock',
    title: 'Lock the remote screen',
    run: () => [keyCmd('control', ControlKey.LockScreen, [])],
  },
  combo('Ctrl+C', 'Copy', ['ctrl'], c('c')),
  combo('Ctrl+V', 'Paste', ['ctrl'], c('v')),
  combo('Ctrl+X', 'Cut', ['ctrl'], c('x')),
  combo('Ctrl+Z', 'Undo', ['ctrl'], c('z')),
  combo('Ctrl+A', 'Select all', ['ctrl'], c('a')),
];
