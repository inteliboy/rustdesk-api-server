// WebSocket transport: one protobuf message per Binary frame (WS framing = NONE).
// Inbound frames are buffered until a consumer attaches, so the handshake can
// `await next()` for the first frame (SignedId) before replying. Consumer
// priority per frame: pending next()/iterator waiter, then onMessage callback,
// then the buffer.
//
// Node harness: either pass the 'ws' package's WebSocket class to
// `WsStream.open(url, WebSocket)`, or open a socket yourself and hand the
// already-OPEN instance to `new WsStream(sock)` (the 'ws' WebSocket is
// addEventListener-compatible and accepts binaryType='arraybuffer').
import type { Transport } from '../core/contracts';

export interface WebSocketLike {
  binaryType: string;
  bufferedAmount?: number;
  send(data: Uint8Array): void;
  close(): void;
  addEventListener(type: 'open' | 'message' | 'close' | 'error', listener: (ev: { data?: unknown }) => void): void;
}

export type WebSocketCtor = new (url: string) => WebSocketLike;

type Waiter = { resolve: (b: Uint8Array) => void; reject: (e: Error) => void };

function toBytes(data: unknown): Uint8Array {
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  throw new Error(`unexpected websocket frame payload: ${Object.prototype.toString.call(data)}`);
}

export class WsStream implements Transport {
  private readonly ws: WebSocketLike;
  private readonly queue: Uint8Array[] = [];
  private readonly waiters: Waiter[] = [];
  private readonly closeCbs: Array<() => void> = [];
  private msgCb: ((b: Uint8Array) => void) | null = null;
  private closed = false;

  constructor(ws: WebSocketLike) {
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.addEventListener('message', (ev) => this.deliver(toBytes(ev.data)));
    ws.addEventListener('close', () => this.teardown());
    ws.addEventListener('error', () => this.teardown());
  }

  static open(url: string, WsCtor?: WebSocketCtor): Promise<WsStream> {
    const Ctor = WsCtor ?? (globalThis as { WebSocket?: WebSocketCtor }).WebSocket;
    if (!Ctor) return Promise.reject(new Error('no WebSocket constructor available (pass one to WsStream.open)'));
    return new Promise((resolve, reject) => {
      const ws = new Ctor(url);
      ws.binaryType = 'arraybuffer';
      ws.addEventListener('error', () => reject(new Error(`websocket error before open: ${url}`)));
      ws.addEventListener('close', () => reject(new Error(`websocket closed before open: ${url}`)));
      ws.addEventListener('open', () => resolve(new WsStream(ws)));
    });
  }

  send(bytes: Uint8Array): void {
    if (this.closed) throw new Error('send on closed websocket');
    this.ws.send(bytes);
  }

  // Bytes queued in the socket's send buffer — upload flow control reads this.
  buffered(): number {
    return this.ws.bufferedAmount ?? 0;
  }

  onMessage(cb: (b: Uint8Array) => void): void {
    this.msgCb = cb;
    while (this.queue.length > 0 && this.msgCb === cb) cb(this.queue.shift()!);
  }

  onClose(cb: () => void): void {
    if (this.closed) { cb(); return; }
    this.closeCbs.push(cb);
  }

  // Resolves with the next inbound frame; drains buffered frames first (even
  // after close), then rejects once the socket is closed and the buffer empty.
  next(): Promise<Uint8Array> {
    const buffered = this.queue.shift();
    if (buffered) return Promise.resolve(buffered);
    if (this.closed) return Promise.reject(new Error('websocket closed'));
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<Uint8Array, void, void> {
    for (;;) {
      try {
        yield await this.next();
      } catch {
        return; // next() rejects only on close
      }
    }
  }

  close(): void {
    if (this.closed) return;
    this.ws.close();
    this.teardown();
  }

  get isClosed(): boolean {
    return this.closed;
  }

  private deliver(b: Uint8Array): void {
    const waiter = this.waiters.shift();
    if (waiter) { waiter.resolve(b); return; }
    if (this.msgCb) { this.msgCb(b); return; }
    this.queue.push(b);
  }

  private teardown(): void {
    if (this.closed) return;
    this.closed = true;
    for (const w of this.waiters.splice(0)) w.reject(new Error('websocket closed'));
    for (const cb of this.closeCbs.splice(0)) cb();
  }
}
