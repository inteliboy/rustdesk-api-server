// Fallback video path for origins where WebCodecs is unavailable.
//
// WebCodecs' VideoDecoder only exists in a secure context, which is what stops
// the normal pipeline running over plain http://. Media Source Extensions is
// NOT secure-context gated, so H.264 can still be played — just not decoded
// frame-by-frame into a canvas.
//
// That difference forces a different shape. The WebCodecs path decodes inside
// the worker and paints an OffscreenCanvas. MSE needs a real <video> element,
// which only exists on the main thread, so here the worker does no decoding at
// all: it forwards the H.264 Annex B bitstream, and the main thread muxes it
// into fragmented MP4 (the only thing MSE accepts) and feeds a <video>.
//
// The trade is real and the user is told about it: H.264 only — no VP8, VP9 or
// AV1 — and no per-frame stats, because nothing here ever sees a frame.

import type { EncodedVideoFrame, VideoFrame as ProtoVideoFrame } from '../gen/message';
import type { EncodedCase } from './video';

/**
 * Worker side: implements the same shape as VideoPipeline but decodes nothing.
 *
 * Non-H.264 frames are dropped rather than queued. The probe advertises H.264
 * alone in this mode, so anything else means the peer ignored the advertisement
 * — worth a refresh request, not worth buffering.
 */
export class ForwardingVideoPipeline {
  onNeedReadvertise: ((disabled: EncodedCase) => void) | null = null;

  private closed = false;
  private seenKey = false;

  constructor(
    private readonly emit: (data: Uint8Array, key: boolean) => void,
    private readonly onNeedRefresh: () => void,
  ) {}

  disabledCodecs(): EncodedCase[] {
    // Everything except H.264 — the peer is told this via supported_decoding,
    // and repeating it here keeps re-advertisement consistent.
    return ['vp8s', 'vp9s', 'av1s', 'h265s'];
  }

  pushFrame(vf: ProtoVideoFrame): void {
    if (this.closed) return;
    const u = vf.union;
    if (!u) return;
    if (u.$case !== 'h264s') {
      this.onNeedRefresh();
      return;
    }
    for (const f of u.h264s.frames as EncodedVideoFrame[]) {
      // A decoder cannot start mid-GOP; hold until the first key frame so the
      // muxer is never handed a delta with no reference.
      if (!this.seenKey) {
        if (!f.key) {
          this.onNeedRefresh();
          continue;
        }
        this.seenKey = true;
      }
      this.emit(f.data, f.key);
    }
  }

  reset(): void {
    this.seenKey = false;
  }

  close(): void {
    this.closed = true;
  }
}

/**
 * Main-thread side: H.264 Annex B in, moving picture out.
 *
 * jMuxer (MIT) does the Annex B -> fragmented MP4 work and drives the
 * MediaSource; writing that muxer by hand is a large amount of exacting code
 * for no gain.
 */
export class MseVideoPlayer {
  private muxer: import('jmuxer').default | undefined;
  private starting: Promise<void> | undefined;

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly onError: (message: string) => void,
  ) {}

  /**
   * jMuxer is loaded on demand: it is only ever needed on an insecure origin,
   * and this keeps it out of the bundle's start-up path for everyone else.
   */
  private async ensure(): Promise<void> {
    if (this.muxer) return;
    this.starting ??= (async () => {
      // jMuxer ships a UMD bundle whose header picks its export style from
      // what globals it sees. Bundled for the browser it takes the third
      // branch — `global.JMuxer = factory(...)` — so the module object comes
      // back EMPTY and the class arrives on globalThis instead. Importing for
      // the side effect and then resolving from either place is the only
      // reliable read; assuming `default` fails as the opaque "e is not a
      // constructor" once minified.
      const mod = (await import('jmuxer')) as unknown as Record<string, unknown>;
      const g = globalThis as unknown as Record<string, unknown>;
      const candidates = [
        mod['default'],
        (mod['default'] as Record<string, unknown> | undefined)?.['default'],
        g['JMuxer'],
      ];
      const JMuxer = candidates.find((c) => typeof c === 'function') as
        | (new (o: unknown) => import('jmuxer').default)
        | undefined;
      if (!JMuxer) throw new Error('jmuxer: no constructor on the module or globalThis');
      this.muxer = new JMuxer({
        node: this.video,
        mode: 'video',
        flushingTime: 0, // append as frames arrive; this is a live stream
        clearBuffer: true, // drop played data, or a long session grows forever
        fps: 30,
        debug: false,
        onError: () => this.onError('Video stream error — requesting a fresh key frame'),
      });
    })();
    await this.starting;
  }

  push(data: Uint8Array, _key: boolean): void {
    void this.ensure().then(() => {
      // feed() copies what it needs; the buffer may be reused after return.
      this.muxer?.feed({ video: data });
      // Autoplay on a muted element is permitted without a gesture; the
      // element is muted because audio arrives on its own path, not here.
      if (this.video.paused) void this.video.play().catch(() => undefined);
    });
  }

  close(): void {
    try {
      this.muxer?.destroy();
    } catch {
      /* destroy after a torn-down MediaSource can throw; nothing to salvage */
    }
    this.muxer = undefined;
    this.starting = undefined;
  }
}
