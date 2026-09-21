// jMuxer ships no types. Only the surface this client uses is declared —
// a fuller definition would be guesswork about a dependency we call in
// exactly one place.
declare module 'jmuxer' {
  interface JMuxerOptions {
    node: HTMLVideoElement | string;
    mode?: 'video' | 'audio' | 'both';
    flushingTime?: number;
    clearBuffer?: boolean;
    fps?: number;
    debug?: boolean;
    onReady?: () => void;
    onError?: (e: unknown) => void;
  }
  export default class JMuxer {
    constructor(options: JMuxerOptions);
    feed(data: { video?: Uint8Array; audio?: Uint8Array; duration?: number }): void;
    destroy(): void;
  }
}
