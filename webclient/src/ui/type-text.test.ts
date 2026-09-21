import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { UiCommand } from '../core/contracts';
import { TYPING_SPEEDS, TextTyper } from './type-text';

function values(cmds: UiCommand[]): number[] {
  return cmds.map((c) => (c.c === 'key' ? c.value : -1));
}

describe('TextTyper', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('sends every key of the text, in order, in batches', () => {
    const sent: UiCommand[] = [];
    const typer = new TextTyper((c) => sent.push(c));
    const progress: number[] = [];
    let done: boolean | undefined;
    const text = 'a'.repeat(25);
    typer.start({ text, speed: 'normal', enter: false, onProgress: (n) => progress.push(n), onDone: (ok) => (done = ok) });
    expect(sent).toHaveLength(TYPING_SPEEDS.normal.chunk); // the first batch goes at once
    expect(typer.busy).toBe(true);
    vi.advanceTimersByTime(TYPING_SPEEDS.normal.everyMs * 5);
    expect(sent).toHaveLength(25);
    expect(progress).toEqual([10, 20, 25]);
    expect(done).toBe(true);
    expect(typer.busy).toBe(false);
  });

  it('presses Enter at the end only when asked', () => {
    expect(values(TextTyper.commandsFor('hi', false))).toEqual([104, 105]);
    expect(values(TextTyper.commandsFor('hi', true))).toEqual([104, 105, 27]);
  });

  it('turns a newline into Enter, a tab into Tab, and non-ASCII into unicode keys', () => {
    const cmds = TextTyper.commandsFor('a\r\nb\té', false);
    expect(values(cmds)).toEqual([97, 27, 98, 31, 233]);
    expect(cmds.map((c) => (c.c === 'key' ? c.keyKind : ''))).toEqual(['chr', 'control', 'chr', 'control', 'unicode']);
  });

  it('stops where it is and sends nothing more', () => {
    const sent: UiCommand[] = [];
    const typer = new TextTyper((c) => sent.push(c));
    let done: boolean | undefined;
    typer.start({ text: 'b'.repeat(100), speed: 'slow', enter: false, onProgress: () => {}, onDone: (ok) => (done = ok) });
    vi.advanceTimersByTime(TYPING_SPEEDS.slow.everyMs);
    const before = sent.length;
    typer.stop();
    vi.advanceTimersByTime(10_000);
    expect(sent).toHaveLength(before);
    expect(done).toBe(false);
    expect(typer.busy).toBe(false);
  });

  it('a new run replaces a running one', () => {
    const sent: UiCommand[] = [];
    const typer = new TextTyper((c) => sent.push(c));
    const ends: boolean[] = [];
    typer.start({ text: 'x'.repeat(50), speed: 'slow', enter: false, onProgress: () => {}, onDone: (ok) => ends.push(ok) });
    typer.start({ text: 'y', speed: 'fast', enter: false, onProgress: () => {}, onDone: (ok) => ends.push(ok) });
    expect(ends).toEqual([false, true]);
    vi.advanceTimersByTime(10_000);
    expect(values(sent.slice(-1))).toEqual([121]);
  });

  it('an empty text is done at once', () => {
    let done: boolean | undefined;
    const typer = new TextTyper(() => {});
    typer.start({ text: '', speed: 'fast', enter: false, onProgress: () => {}, onDone: (ok) => (done = ok) });
    expect(done).toBe(true);
    expect(typer.busy).toBe(false);
  });
});
