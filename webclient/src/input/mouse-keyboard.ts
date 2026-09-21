import { ControlKey } from '../gen/message';
import type { UiCommand } from '../core/contracts';

export const MouseType = { MOVE: 0, DOWN: 1, UP: 2, WHEEL: 3, TRACKPAD: 4, MOVE_RELATIVE: 5 } as const;
export const MouseButton = { LEFT: 0x01, RIGHT: 0x02, MIDDLE: 0x04, BACK: 0x08, FORWARD: 0x10 } as const;

export type DisplayRect = { x: number; y: number; width: number; height: number };

// RustDesk MouseEvent.mask layout: low 3 bits = event type, buttons shifted left by 3.
export function buttonMask(type: number, buttons: number): number {
  return (buttons << 3) | type;
}

const clamp01 = (v: number): number => (v < 0 ? 0 : v > 1 ? 1 : v);

// Canvas-local pixel -> absolute virtual-desktop coordinate on the current display.
// The canvas renders with object-fit: contain, so the video image is letterboxed
// (centered, with bars) whenever the element aspect differs from the display
// aspect. Map the click against the ACTUAL rendered image rect — not the element
// box — otherwise the coordinate drifts toward center (clicks land left/high).
export function mapCoords(
  clientX: number,
  clientY: number,
  canvasRect: DOMRect,
  display: DisplayRect,
): { x: number; y: number } {
  const { width: rw, height: rh, left, top } = canvasRect;
  if (display.width <= 0 || display.height <= 0 || rw <= 0 || rh <= 0) {
    return { x: Math.round(display.x), y: Math.round(display.y) };
  }
  const dispAspect = display.width / display.height;
  const elAspect = rw / rh;
  let contentW = rw;
  let contentH = rh;
  if (elAspect > dispAspect) {
    contentW = rh * dispAspect; // pillarbox: bars left/right
  } else {
    contentH = rw / dispAspect; // letterbox: bars top/bottom
  }
  const offX = (rw - contentW) / 2;
  const offY = (rh - contentH) / 2;
  const fx = clamp01((clientX - left - offX) / contentW);
  const fy = clamp01((clientY - top - offY) / contentH);
  return {
    x: Math.round(display.x + fx * display.width),
    y: Math.round(display.y + fy * display.height),
  };
}

export function domButtonToMask(domButton: number): number {
  switch (domButton) {
    case 0: return MouseButton.LEFT;
    case 1: return MouseButton.MIDDLE;
    case 2: return MouseButton.RIGHT;
    case 3: return MouseButton.BACK;
    case 4: return MouseButton.FORWARD;
    default: return 0;
  }
}

// RustDesk wheel convention is inverted relative to DOM deltaY.
export function wheelDelta(deltaY: number): number {
  return deltaY > 0 ? -1 : deltaY < 0 ? 1 : 0;
}

const NAMED_KEYS: Record<string, ControlKey> = {
  Enter: ControlKey.Return,
  Backspace: ControlKey.Backspace,
  Tab: ControlKey.Tab,
  Escape: ControlKey.Escape,
  Delete: ControlKey.Delete,
  Insert: ControlKey.Insert,
  Home: ControlKey.Home,
  End: ControlKey.End,
  PageUp: ControlKey.PageUp,
  PageDown: ControlKey.PageDown,
  ArrowUp: ControlKey.UpArrow,
  ArrowDown: ControlKey.DownArrow,
  ArrowLeft: ControlKey.LeftArrow,
  ArrowRight: ControlKey.RightArrow,
  ' ': ControlKey.Space,
  Shift: ControlKey.Shift,
  Control: ControlKey.Control,
  Alt: ControlKey.Alt,
  Meta: ControlKey.Meta,
  AltGraph: ControlKey.RAlt,
  CapsLock: ControlKey.CapsLock,
  NumLock: ControlKey.NumLock,
  ScrollLock: ControlKey.Scroll,
  Pause: ControlKey.Pause,
  PrintScreen: ControlKey.Snapshot,
  ContextMenu: ControlKey.Apps,
  Help: ControlKey.Help,
  Clear: ControlKey.Clear,
  Select: ControlKey.Select,
  Execute: ControlKey.Execute,
  Print: ControlKey.Print,
  Cancel: ControlKey.Cancel,
  Convert: ControlKey.Convert,
  F1: ControlKey.F1,
  F2: ControlKey.F2,
  F3: ControlKey.F3,
  F4: ControlKey.F4,
  F5: ControlKey.F5,
  F6: ControlKey.F6,
  F7: ControlKey.F7,
  F8: ControlKey.F8,
  F9: ControlKey.F9,
  F10: ControlKey.F10,
  F11: ControlKey.F11,
  F12: ControlKey.F12,
};

const RIGHT_VARIANTS: Record<string, ControlKey> = {
  Shift: ControlKey.RShift,
  Control: ControlKey.RControl,
  Alt: ControlKey.RAlt,
  Meta: ControlKey.RWin,
};

const NUMPAD_KEYS: Record<string, ControlKey> = {
  '0': ControlKey.Numpad0,
  '1': ControlKey.Numpad1,
  '2': ControlKey.Numpad2,
  '3': ControlKey.Numpad3,
  '4': ControlKey.Numpad4,
  '5': ControlKey.Numpad5,
  '6': ControlKey.Numpad6,
  '7': ControlKey.Numpad7,
  '8': ControlKey.Numpad8,
  '9': ControlKey.Numpad9,
  '+': ControlKey.Add,
  '-': ControlKey.Subtract,
  '*': ControlKey.Multiply,
  '/': ControlKey.Divide,
  '.': ControlKey.Decimal,
  '=': ControlKey.Equals,
  Enter: ControlKey.NumpadEnter,
};

const LOCATION_RIGHT = 2; // KeyboardEvent.DOM_KEY_LOCATION_RIGHT
const LOCATION_NUMPAD = 3; // KeyboardEvent.DOM_KEY_LOCATION_NUMPAD

// Legacy keyboard mode: printable char -> chr codepoint (ASCII) or unicode
// codepoint (non-ASCII), named key -> ControlKey. null = ignore (Dead, IME, ...).
export function legacyKeyCommand(
  e: KeyboardEvent,
  _down: boolean,
): { keyKind: 'chr' | 'control' | 'unicode'; value: number } | null {
  const key = e.key;
  if (e.location === LOCATION_NUMPAD) {
    const np = NUMPAD_KEYS[key];
    if (np !== undefined) return { keyKind: 'control', value: np };
  }
  if (e.location === LOCATION_RIGHT) {
    const rk = RIGHT_VARIANTS[key];
    if (rk !== undefined) return { keyKind: 'control', value: rk };
  }
  const named = NAMED_KEYS[key];
  if (named !== undefined) return { keyKind: 'control', value: named };
  const cps = Array.from(key);
  if (cps.length !== 1) return null; // Dead, Unidentified, Process, unmapped named keys
  const cp = cps[0].codePointAt(0)!;
  return cp <= 0x7f ? { keyKind: 'chr', value: cp } : { keyKind: 'unicode', value: cp };
}

type ModifierState = { ctrlKey: boolean; shiftKey: boolean; altKey: boolean; metaKey: boolean };

function modifierList(e: ModifierState): number[] {
  const m: number[] = [];
  if (e.ctrlKey) m.push(ControlKey.Control);
  if (e.shiftKey) m.push(ControlKey.Shift);
  if (e.altKey) m.push(ControlKey.Alt);
  if (e.metaKey) m.push(ControlKey.Meta);
  return m;
}

type KeyboardLock = { lock?: (keyCodes?: string[]) => Promise<void>; unlock?: () => void };

export type InputOptions = {
  /**
   * When true, touch pointers get gesture mapping: dragging a finger moves the
   * remote cursor without holding a button, a quick tap clicks where the
   * finger was, and a still long-press right-clicks.
   *
   * Without it (pointer mode), a finger behaves like a pressed left button —
   * touching immediately mouses-down and dragging drags. That is correct for
   * a stylus or a touch-screen laptop, and hopeless on a phone, where the
   * finger is also the only way to aim: every attempt to position the cursor
   * drags whatever it lands on. Mouse/pen pointers are identical in both
   * modes.
   */
  isTouchMode?: () => boolean;
};

const TOUCH_TAP_MS = 500;      // tap = shorter than this…
const TOUCH_SLOP_PX = 12;      // …and moved less than this
const TOUCH_LONGPRESS_MS = 600; // still for this long = right-click

export function attachInput(
  el: HTMLElement,
  post: (cmd: UiCommand) => void,
  getDisplay: () => DisplayRect,
  options: InputOptions = {},
): () => void {
  if (el.tabIndex < 0) el.tabIndex = 0; // keyboard focus target

  // One-finger gesture state (touch mode). Secondary touches are ignored — a
  // second finger mid-gesture would otherwise teleport the cursor.
  let touch: {
    id: number; startX: number; startY: number; lastX: number; lastY: number;
    moved: boolean; downAt: number; timer: ReturnType<typeof setTimeout> | undefined;
  } | null = null;

  const touchActive = (e: PointerEvent): boolean =>
    e.pointerType === 'touch' && options.isTouchMode?.() === true;

  const sendAt = (clientX: number, clientY: number, mask: number): void => {
    const { x, y } = mapCoords(clientX, clientY, el.getBoundingClientRect(), getDisplay());
    post({ c: 'mouse', mask, x, y, modifiers: [] });
  };

  const posted = (e: { clientX: number; clientY: number } & ModifierState, mask: number): void => {
    const { x, y } = mapCoords(e.clientX, e.clientY, el.getBoundingClientRect(), getDisplay());
    post({ c: 'mouse', mask, x, y, modifiers: modifierList(e) });
  };

  const onPointerMove = (e: PointerEvent): void => {
    if (touchActive(e)) {
      if (!touch || e.pointerId !== touch.id) return;
      const dx = e.clientX - touch.startX;
      const dy = e.clientY - touch.startY;
      if (!touch.moved && dx * dx + dy * dy > TOUCH_SLOP_PX * TOUCH_SLOP_PX) {
        touch.moved = true;
        clearTimeout(touch.timer);
      }
      touch.lastX = e.clientX;
      touch.lastY = e.clientY;
      sendAt(e.clientX, e.clientY, buttonMask(MouseType.MOVE, 0)); // cursor follows, no button
      return;
    }
    posted(e, buttonMask(MouseType.MOVE, 0));
  };
  const onPointerDown = (e: PointerEvent): void => {
    el.focus();
    try {
      el.setPointerCapture(e.pointerId);
    } catch {
      /* capture is best-effort */
    }
    e.preventDefault();
    if (touchActive(e)) {
      if (touch) return; // second finger — ignore
      touch = {
        id: e.pointerId, startX: e.clientX, startY: e.clientY,
        lastX: e.clientX, lastY: e.clientY, moved: false, downAt: Date.now(),
        timer: setTimeout(() => {
          // Long-press, finger still: context menu where the finger rests.
          if (!touch || touch.moved) return;
          sendAt(touch.lastX, touch.lastY, buttonMask(MouseType.MOVE, 0));
          sendAt(touch.lastX, touch.lastY, buttonMask(MouseType.DOWN, MouseButton.RIGHT));
          sendAt(touch.lastX, touch.lastY, buttonMask(MouseType.UP, MouseButton.RIGHT));
          touch = null; // consumed; the pointerup that follows does nothing
        }, TOUCH_LONGPRESS_MS),
      };
      return;
    }
    posted(e, buttonMask(MouseType.DOWN, domButtonToMask(e.button)));
  };
  const onPointerUp = (e: PointerEvent): void => {
    e.preventDefault();
    if (e.pointerType === 'touch' && touch && e.pointerId === touch.id) {
      clearTimeout(touch.timer);
      const wasTap = !touch.moved && Date.now() - touch.downAt < TOUCH_TAP_MS;
      const { startX, startY } = touch;
      touch = null;
      if (wasTap) {
        sendAt(startX, startY, buttonMask(MouseType.MOVE, 0));
        sendAt(startX, startY, buttonMask(MouseType.DOWN, MouseButton.LEFT));
        sendAt(startX, startY, buttonMask(MouseType.UP, MouseButton.LEFT));
      }
      return;
    }
    if (touchActive(e)) return; // stray secondary finger
    posted(e, buttonMask(MouseType.UP, domButtonToMask(e.button)));
  };
  const onWheel = (e: WheelEvent): void => {
    e.preventDefault();
    const dx = wheelDelta(e.deltaX);
    const dy = wheelDelta(e.deltaY);
    if (dx === 0 && dy === 0) return;
    // WHEEL events carry scroll steps in x/y, not coordinates.
    post({ c: 'mouse', mask: buttonMask(MouseType.WHEEL, 0), x: dx, y: dy, modifiers: modifierList(e) });
  };
  const onContextMenu = (e: Event): void => {
    e.preventDefault();
  };

  const onKey = (down: boolean) => (e: KeyboardEvent): void => {
    const k = legacyKeyCommand(e, down);
    if (!k) return;
    e.preventDefault(); // keeps Ctrl+W / F5 / Tab from hitting the browser while focused
    post({ c: 'key', down, press: false, keyKind: k.keyKind, value: k.value, modifiers: modifierList(e) });
  };
  const onKeyDown = onKey(true);
  const onKeyUp = onKey(false);

  const kb: KeyboardLock | undefined =
    typeof navigator !== 'undefined' && 'keyboard' in navigator
      ? (navigator as Navigator & { keyboard?: KeyboardLock }).keyboard
      : undefined;
  const onFullscreenChange = (): void => {
    if (!kb?.lock) return;
    const fs = document.fullscreenElement;
    if (fs && (fs === el || fs.contains(el))) {
      void kb.lock().catch(() => undefined);
    } else {
      kb.unlock?.();
    }
  };

  el.addEventListener('pointermove', onPointerMove);
  el.addEventListener('pointerdown', onPointerDown);
  el.addEventListener('pointerup', onPointerUp);
  el.addEventListener('wheel', onWheel, { passive: false });
  el.addEventListener('contextmenu', onContextMenu);
  el.addEventListener('keydown', onKeyDown);
  el.addEventListener('keyup', onKeyUp);
  document.addEventListener('fullscreenchange', onFullscreenChange);

  return () => {
    el.removeEventListener('pointermove', onPointerMove);
    el.removeEventListener('pointerdown', onPointerDown);
    el.removeEventListener('pointerup', onPointerUp);
    el.removeEventListener('wheel', onWheel);
    el.removeEventListener('contextmenu', onContextMenu);
    el.removeEventListener('keydown', onKeyDown);
    el.removeEventListener('keyup', onKeyUp);
    document.removeEventListener('fullscreenchange', onFullscreenChange);
    kb?.unlock?.();
  };
}
