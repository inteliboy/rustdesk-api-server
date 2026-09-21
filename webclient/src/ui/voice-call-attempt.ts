export type OwnedAttemptResult<T> =
  | { owned: true; value: T }
  | { owned: false };

/** Owns async voice-call preparations so stale completions cannot mutate a replacement call. */
export class VoiceCallAttemptOwner {
  private generation = 0;

  begin(): number {
    return ++this.generation;
  }

  invalidate(): void {
    this.generation++;
  }

  async wait<T>(token: number, work: () => Promise<T>): Promise<OwnedAttemptResult<T>> {
    const value = await work();
    return token === this.generation ? { owned: true, value } : { owned: false };
  }
}
