// A window over the remote screen that the person can move by its title bar and that never leaves the screen area,
// so the bar can always be grabbed again. The offset is kept in two CSS variables (`<prefix>-dx`, `<prefix>-dy`)
// that the window's stylesheet turns into a translate.

export class WindowDrag {
  private dx = 0;
  private dy = 0;

  constructor(
    private readonly host: HTMLElement,
    private readonly viewport: HTMLElement,
    private readonly prefix: string,
  ) {}

  /** Make `handle` (the title bar) move the window; buttons inside it stay buttons. */
  attach(handle: HTMLElement): void {
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || (e.target as HTMLElement).closest('button, select, input, textarea')) return;
      e.preventDefault();
      const start = { x: e.clientX, y: e.clientY, dx: this.dx, dy: this.dy };
      handle.setPointerCapture(e.pointerId);
      this.host.classList.add('rd-dragging');
      const move = (ev: PointerEvent): void => {
        this.dx = start.dx + (ev.clientX - start.x);
        this.dy = start.dy + (ev.clientY - start.y);
        this.clamp();
      };
      const done = (): void => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', done);
        handle.removeEventListener('pointercancel', done);
        this.host.classList.remove('rd-dragging');
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', done);
      handle.addEventListener('pointercancel', done);
    });
  }

  /** Apply the offset, pulled back inside the remote screen area if it has left it. */
  clamp(): void {
    this.apply();
    const room = this.viewport.getBoundingClientRect();
    const box = this.host.getBoundingClientRect();
    if (!room.width || !box.width) return;
    const fixX = Math.max(room.left - box.left, Math.min(0, room.right - box.right));
    const fixY = Math.max(room.top - box.top, Math.min(0, room.bottom - box.bottom));
    if (fixX || fixY) {
      this.dx += fixX;
      this.dy += fixY;
      this.apply();
    }
  }

  private apply(): void {
    this.host.style.setProperty(`${this.prefix}-dx`, `${this.dx}px`);
    this.host.style.setProperty(`${this.prefix}-dy`, `${this.dy}px`);
  }
}
