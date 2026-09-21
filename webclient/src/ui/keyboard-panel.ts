// The on-screen keyboard: a small window over the remote screen with a full keyboard, the modifier keys as
// latches (tap = the next key or click only, tap twice = held, tap again = off) and one-tap shortcuts.
// What the keys mean lives in input/virtual-keyboard.ts; this file draws it and forwards taps.
import type { UiCommand } from '../core/contracts';
import {
  MAIN_LAYOUT,
  NAV_LAYOUT,
  SHORTCUTS,
  commandsForKey,
  isGap,
  type ModifierLatches,
  type Slot,
  type VKey,
} from '../input/virtual-keyboard';
import { iconHtml } from './common';

/** A held key repeats after this long, then this often. */
const REPEAT_DELAY_MS = 400;
const REPEAT_EVERY_MS = 60;

export type KeyboardPanelOptions = {
  /** The window's element, already inside the viewport. */
  host: HTMLElement;
  viewport: HTMLElement;
  latches: ModifierLatches;
  send: (cmd: UiCommand) => void;
  onVisibility: (open: boolean) => void;
};

export class KeyboardPanel {
  private readonly keys = new Map<string, { vk: VKey; button: HTMLButtonElement; label: HTMLElement }>();
  private repeatTimer: ReturnType<typeof setTimeout> | undefined;
  private repeatKey: VKey | undefined;
  private dx = 0;
  private dy = 0;

  constructor(private readonly o: KeyboardPanelOptions) {
    this.render();
    o.latches.onChange = () => this.refresh();
  }

  get isOpen(): boolean {
    return this.o.host.classList.contains('rd-open');
  }

  open(): void {
    this.o.host.classList.add('rd-open');
    this.clampPosition();
    this.o.onVisibility(true);
  }

  /** Closing forgets the latches: a modifier nobody can see must not ride along with the next click. */
  close(): void {
    this.stopRepeat();
    const was = this.isOpen;
    this.o.host.classList.remove('rd-open');
    this.o.latches.clear();
    if (was) this.o.onVisibility(false);
  }

  toggle(): void {
    if (this.isOpen) this.close();
    else this.open();
  }

  /** Redraw what the latches change: the modifier keys' state, and the shifted characters. */
  refresh(): void {
    const shift = this.o.latches.state('shift') !== 'off';
    for (const { vk, button, label } of this.keys.values()) {
      if (vk.act.t === 'mod') {
        const st = this.o.latches.state(vk.act.mod);
        button.classList.toggle('rd-once', st === 'once');
        button.classList.toggle('rd-locked', st === 'locked');
        button.setAttribute('aria-pressed', String(st !== 'off'));
      } else if (vk.act.t === 'chr') {
        label.textContent = shift ? vk.act.shifted : vk.act.ch;
      }
    }
  }

  // --- drawing ----------------------------------------------------------------------

  private slotsHtml(slots: Slot[]): string {
    return slots
      .map((s) => {
        if (isGap(s)) return `<i class="rd-k-gap" style="--w:${s.gap}"></i>`;
        const tip = s.aria ? ` title="${s.aria}" aria-label="${s.aria}"` : '';
        const cls = s.act.t === 'mod' ? 'rd-k rd-k-mod' : s.act.t === 'chr' ? 'rd-k rd-k-chr' : 'rd-k';
        const pressed = s.act.t === 'mod' ? ' aria-pressed="false"' : '';
        return `<button type="button" class="${cls}" data-key="${s.id}" style="--w:${s.w}"${tip}${pressed}><span data-i18n-skip>${escapeText(s.label)}</span></button>`;
      })
      .join('');
  }

  private render(): void {
    const chips = SHORTCUTS.map(
      (s, i) => `<button type="button" class="rd-kbd-chip" data-shortcut="${i}" title="${s.title}"><span data-i18n-skip>${s.label}</span></button>`,
    ).join('');
    this.o.host.innerHTML = `
      <header class="rd-kbd-head" id="rd-kbd-head">
        <span class="rd-kbd-title">${iconHtml('keyboard')}<span>Keyboard</span></span>
        <button type="button" class="rd-ib" id="rd-kbd-close" title="Close keyboard" aria-label="Close keyboard">${iconHtml('close')}</button>
      </header>
      <div class="rd-kbd-body">
        <div class="rd-kbd-chips" role="group" aria-label="Shortcuts">${chips}</div>
        <div class="rd-kbd-keys">
          <div class="rd-kbd-main">${this.slotsHtml(MAIN_LAYOUT)}</div>
          <div class="rd-kbd-nav">${this.slotsHtml(NAV_LAYOUT)}</div>
        </div>
      </div>`;
    const byId = new Map<string, VKey>();
    for (const s of [...MAIN_LAYOUT, ...NAV_LAYOUT]) if (!isGap(s)) byId.set(s.id, s);
    for (const button of this.o.host.querySelectorAll<HTMLButtonElement>('[data-key]')) {
      const vk = byId.get(button.dataset.key ?? '');
      if (vk) this.keys.set(vk.id, { vk, button, label: button.firstElementChild as HTMLElement });
    }
    this.wire();
  }

  private wire(): void {
    const host = this.o.host;
    // Taps must not take the keyboard focus from the remote screen: a physical key pressed next would go nowhere.
    host.addEventListener('mousedown', (e) => {
      if (!(e.target as HTMLElement).closest('.rd-kbd-head')) e.preventDefault();
    });
    host.addEventListener('pointerdown', (e) => {
      const key = this.keyOf(e.target);
      if (!key || e.button !== 0) return;
      this.press(key);
      if (key.repeat && key.act.t !== 'mod') this.startRepeat(key);
    });
    for (const type of ['pointerup', 'pointercancel', 'pointerleave'] as const) {
      host.addEventListener(type, () => this.stopRepeat());
    }
    host.addEventListener('click', (e) => {
      const chip = (e.target as HTMLElement).closest<HTMLElement>('[data-shortcut]');
      if (chip) {
        this.shortcut(Number(chip.dataset.shortcut));
        return;
      }
      // A key's click with no pointer behind it is the keyboard (Tab to a key, Enter or Space); the pointer path ran already.
      if (e.detail !== 0) return;
      const key = this.keyOf(e.target);
      if (key) this.press(key);
    });
    q(host, '#rd-kbd-close').addEventListener('click', () => this.close());
    this.wireDrag(q(host, '#rd-kbd-head'));
  }

  private keyOf(target: EventTarget | null): VKey | undefined {
    const id = (target as HTMLElement | null)?.closest<HTMLElement>('[data-key]')?.dataset.key;
    return id ? this.keys.get(id)?.vk : undefined;
  }

  // --- pressing ---------------------------------------------------------------------

  private press(vk: VKey): void {
    if (vk.act.t === 'mod') {
      this.o.latches.tap(vk.act.mod, Date.now());
      return;
    }
    // The commands are built with the latches as they are; the app merges them again and then uses up the one-shot ones.
    for (const cmd of commandsForKey(vk.act, this.o.latches.active())) this.o.send(cmd);
  }

  private shortcut(index: number): void {
    const s = SHORTCUTS[index];
    if (!s) return;
    // A shortcut is a whole combination: nothing latched may join it.
    this.o.latches.clear();
    for (const cmd of s.run()) this.o.send(cmd);
  }

  private startRepeat(vk: VKey): void {
    this.stopRepeat();
    this.repeatKey = vk;
    this.repeatTimer = setTimeout(() => {
      this.repeatTimer = setInterval(() => {
        if (this.repeatKey) this.press(this.repeatKey);
      }, REPEAT_EVERY_MS);
    }, REPEAT_DELAY_MS);
  }

  private stopRepeat(): void {
    // The same handle holds the delay, then the interval; clearing with either function stops both.
    clearTimeout(this.repeatTimer);
    clearInterval(this.repeatTimer);
    this.repeatTimer = undefined;
    this.repeatKey = undefined;
  }

  // --- moving -----------------------------------------------------------------------

  private wireDrag(handle: HTMLElement): void {
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || (e.target as HTMLElement).closest('button')) return;
      e.preventDefault();
      const start = { x: e.clientX, y: e.clientY, dx: this.dx, dy: this.dy };
      handle.setPointerCapture(e.pointerId);
      this.o.host.classList.add('rd-dragging');
      const move = (ev: PointerEvent): void => {
        this.dx = start.dx + (ev.clientX - start.x);
        this.dy = start.dy + (ev.clientY - start.y);
        this.clampPosition();
      };
      const done = (): void => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', done);
        handle.removeEventListener('pointercancel', done);
        this.o.host.classList.remove('rd-dragging');
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', done);
      handle.addEventListener('pointercancel', done);
    });
  }

  /** The window stays inside the remote screen area, so its title bar can always be grabbed again. */
  private clampPosition(): void {
    const host = this.o.host;
    host.style.setProperty('--rd-kbd-dx', `${this.dx}px`);
    host.style.setProperty('--rd-kbd-dy', `${this.dy}px`);
    const room = this.o.viewport.getBoundingClientRect();
    const box = host.getBoundingClientRect();
    if (!room.width || !box.width) return;
    const fixX = Math.max(room.left - box.left, Math.min(0, room.right - box.right));
    const fixY = Math.max(room.top - box.top, Math.min(0, room.bottom - box.bottom));
    if (fixX || fixY) {
      this.dx += fixX;
      this.dy += fixY;
      host.style.setProperty('--rd-kbd-dx', `${this.dx}px`);
      host.style.setProperty('--rd-kbd-dy', `${this.dy}px`);
    }
  }
}

function q<T extends HTMLElement = HTMLElement>(root: ParentNode, sel: string): T {
  const el = root.querySelector<T>(sel);
  if (!el) throw new Error(`webclient: missing element ${sel}`);
  return el;
}

function escapeText(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
