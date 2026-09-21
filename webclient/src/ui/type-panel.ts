// The Type window: a movable window over the remote screen with a text area to write or paste into, sent to the
// remote device as key presses. It works where the clipboard does not (a sign-in screen, a password field, a
// session inside a session). What is written never leaves this page except as those key presses, and it is not
// kept: the text is cleared once it has been typed, when the window is closed and when the session ends.
import type { UiCommand } from '../core/contracts';
import { iconHtml } from './common';
import { TextTyper, type TypingSpeed } from './type-text';
import { WindowDrag } from './window-drag';

const SPEED_KEY = 'rd_type_speed';
const ENTER_KEY = 'rd_type_enter';

export type TypePanelOptions = {
  /** The window's element, already inside the viewport. */
  host: HTMLElement;
  viewport: HTMLElement;
  send: (cmd: UiCommand) => void;
  toast: (message: string) => void;
  onVisibility: (open: boolean) => void;
  /** Called just before typing starts: whatever was latched on the on-screen keyboard must not join the text. */
  beforeTyping: () => void;
};

export class TypePanel {
  private readonly typer: TextTyper;
  private readonly drag: WindowDrag;
  private text!: HTMLTextAreaElement;
  private count!: HTMLElement;
  private sendBtn!: HTMLButtonElement;
  private enter!: HTMLInputElement;
  private speed!: HTMLSelectElement;
  private total = 0;

  constructor(private readonly o: TypePanelOptions) {
    this.typer = new TextTyper((cmd) => o.send(cmd));
    this.drag = new WindowDrag(o.host, o.viewport, '--rd-type');
    this.render();
  }

  get isOpen(): boolean {
    return this.o.host.classList.contains('rd-open');
  }

  open(): void {
    this.o.host.classList.add('rd-open');
    this.drag.clamp();
    this.o.onVisibility(true);
    this.text.focus();
  }

  /** Closing stops a run in progress and forgets the text. */
  close(): void {
    const was = this.isOpen;
    this.typer.stop();
    this.text.value = '';
    this.updateState();
    this.o.host.classList.remove('rd-open');
    if (was) this.o.onVisibility(false);
  }

  toggle(): void {
    if (this.isOpen) this.close();
    else this.open();
  }

  private render(): void {
    const speed = readSetting(SPEED_KEY);
    this.o.host.innerHTML = `
      <header class="rd-win-head" id="rd-type-head">
        <span class="rd-win-title">${iconHtml('typeText')}<span>Type text</span></span>
        <button type="button" class="rd-ib" id="rd-type-close" title="Close" aria-label="Close type window">${iconHtml('close')}</button>
      </header>
      <div class="rd-type-body">
        <label class="rd-type-label" for="rd-type-text">Text to type on the remote device</label>
        <textarea id="rd-type-text" rows="8" autocomplete="off" autocapitalize="off" spellcheck="false"
          placeholder="Write or paste the text here. It is typed as key presses, so it works where the clipboard does not: sign-in screens, password fields, remote sessions."></textarea>
        <div class="rd-type-meta"><span id="rd-type-count" aria-live="polite"></span></div>
        <div class="rd-type-options">
          <label class="rd-type-check"><input type="checkbox" id="rd-type-enter"><span>Press Enter afterwards</span></label>
          <label class="rd-type-speed" title="Slow is for remote applications that lose keys"><span>Speed</span>
            <select id="rd-type-speed">
              <option value="fast">Fast</option>
              <option value="normal">Normal</option>
              <option value="slow">Slow</option>
            </select>
          </label>
        </div>
        <div class="rd-type-actions">
          <button type="button" class="rd-type-btn" id="rd-type-clear"><span>Clear</span></button>
          <button type="button" class="rd-type-btn rd-solid" id="rd-type-send" title="Ctrl+Enter"><span>Type on remote device</span></button>
        </div>
      </div>`;
    const h = this.o.host;
    this.text = q(h, '#rd-type-text');
    this.count = q(h, '#rd-type-count');
    this.sendBtn = q(h, '#rd-type-send');
    this.enter = q(h, '#rd-type-enter');
    this.speed = q(h, '#rd-type-speed');
    this.speed.value = speed === 'fast' || speed === 'slow' ? speed : 'normal';
    this.enter.checked = readSetting(ENTER_KEY) === '1';

    this.text.addEventListener('input', () => this.updateState());
    this.speed.addEventListener('change', () => writeSetting(SPEED_KEY, this.speed.value));
    this.enter.addEventListener('change', () => writeSetting(ENTER_KEY, this.enter.checked ? '1' : '0'));
    this.sendBtn.addEventListener('click', () => this.sendOrStop());
    q(h, '#rd-type-clear').addEventListener('click', () => {
      this.text.value = '';
      this.updateState();
      this.text.focus();
    });
    q(h, '#rd-type-close').addEventListener('click', () => this.close());
    h.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        this.close();
      } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        this.sendOrStop();
      }
    });
    this.drag.attach(q(h, '#rd-type-head'));
    this.updateState();
  }

  private sendOrStop(): void {
    if (this.typer.busy) {
      this.typer.stop();
      return;
    }
    const text = this.text.value;
    if (!text) {
      this.text.focus();
      return;
    }
    this.o.beforeTyping();
    this.total = 0;
    this.typer.start({
      text,
      speed: this.speed.value as TypingSpeed,
      enter: this.enter.checked,
      onProgress: (sent, total) => {
        this.total = total;
        this.updateState(sent);
      },
      onDone: (completed) => {
        if (completed) {
          this.text.value = '';
          this.o.toast('Text typed on the remote device');
        } else {
          this.o.toast('Typing stopped');
        }
        this.updateState();
        if (this.isOpen) this.text.focus();
      },
    });
    this.updateState(0);
  }

  /** The count line and the button follow what is going on. */
  private updateState(sent?: number): void {
    const typing = this.typer.busy;
    this.text.readOnly = typing;
    this.speed.disabled = typing;
    this.enter.disabled = typing;
    const label = this.sendBtn.querySelector('span');
    if (label) label.textContent = typing ? 'Stop' : 'Type on remote device';
    this.sendBtn.classList.toggle('rd-solid', !typing);
    this.sendBtn.disabled = !typing && this.text.value === '';
    if (typing) {
      this.count.textContent = `Typing… ${sent ?? 0} of ${this.total || '…'} keys`;
      return;
    }
    const chars = [...this.text.value].length;
    this.count.textContent = chars === 0 ? '' : `${chars} character${chars === 1 ? '' : 's'}`;
  }
}

function readSetting(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // storage can be blocked; the defaults are fine
  }
}

function writeSetting(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // not remembered; nothing depends on it
  }
}

function q<T extends HTMLElement = HTMLElement>(root: ParentNode, sel: string): T {
  const el = root.querySelector<T>(sel);
  if (!el) throw new Error(`webclient: missing element ${sel}`);
  return el;
}
