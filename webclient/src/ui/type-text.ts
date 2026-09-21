// Types a piece of text on the remote device as key presses, at a pace the remote side can keep up with.
//
// Sending every key in one go used to be the whole feature; for a long text that floods the connection and a
// slow remote application drops characters. Here the keys go out in small batches, the person sees how far it
// is, and can stop it.
import type { UiCommand } from '../core/contracts';
import { buildTypeCommands } from './common';

export type TypingSpeed = 'fast' | 'normal' | 'slow';

/** Keys per batch and the pause between batches (so roughly 1600, 250 and 50 keys a second). */
export const TYPING_SPEEDS: Record<TypingSpeed, { chunk: number; everyMs: number }> = {
  fast: { chunk: 40, everyMs: 25 },
  normal: { chunk: 10, everyMs: 40 },
  slow: { chunk: 2, everyMs: 40 },
};

/** ControlKey.Return, fixed by the protocol (see buildTypeCommands). */
const RETURN_KEY = 27;

export type TypingRun = {
  text: string;
  speed: TypingSpeed;
  /** Press Enter after the last character. */
  enter: boolean;
  onProgress: (sent: number, total: number) => void;
  /** `completed` is false when the run was stopped. */
  onDone: (completed: boolean) => void;
};

export class TextTyper {
  private timer: ReturnType<typeof setTimeout> | undefined;
  private run: TypingRun | undefined;
  private commands: UiCommand[] = [];
  private sent = 0;

  constructor(private readonly send: (cmd: UiCommand) => void) {}

  get busy(): boolean {
    return this.run !== undefined;
  }

  /** The keys a run of this text would send. */
  static commandsFor(text: string, enter: boolean): UiCommand[] {
    const cmds = buildTypeCommands(text);
    if (enter) cmds.push({ c: 'key', down: false, press: true, keyKind: 'control', value: RETURN_KEY, modifiers: [] });
    return cmds;
  }

  start(run: TypingRun): void {
    this.stop();
    this.commands = TextTyper.commandsFor(run.text, run.enter);
    if (this.commands.length === 0) {
      run.onDone(true);
      return;
    }
    this.run = run;
    this.sent = 0;
    this.step();
  }

  /** Stop where it is; what was sent stays sent. */
  stop(): void {
    if (!this.run) return;
    this.finish(false);
  }

  private step(): void {
    const run = this.run;
    if (!run) return;
    const { chunk, everyMs } = TYPING_SPEEDS[run.speed];
    const end = Math.min(this.sent + chunk, this.commands.length);
    while (this.sent < end) this.send(this.commands[this.sent++]!);
    run.onProgress(this.sent, this.commands.length);
    if (this.sent >= this.commands.length) {
      this.finish(true);
      return;
    }
    this.timer = setTimeout(() => this.step(), everyMs);
  }

  private finish(completed: boolean): void {
    clearTimeout(this.timer);
    this.timer = undefined;
    const run = this.run;
    this.run = undefined;
    this.commands = [];
    run?.onDone(completed);
  }
}
