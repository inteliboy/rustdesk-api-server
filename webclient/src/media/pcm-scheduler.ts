export interface PcmSchedulerBuffer {
  getChannelData(channel: number): Float32Array;
}

export interface PcmSchedulerSource {
  buffer: PcmSchedulerBuffer | null;
  onended: (() => void) | null;
  connect(destination: unknown): void;
  disconnect(): void;
  start(when: number): void;
  stop(): void;
}

export interface PcmSchedulerContext {
  currentTime: number;
  destination: unknown;
  createBuffer(channels: number, frames: number, sampleRate: number): PcmSchedulerBuffer;
  createBufferSource(): PcmSchedulerSource;
}

export type PcmSchedulerOptions = {
  leadSeconds?: number;
  maxQueuedSeconds?: number;
};

/**
 * Small main-thread Web Audio scheduler for decoded, interleaved PCM blocks.
 * AudioBuffer keeps its original sample rate; Web Audio resamples it for the
 * output device when necessary.
 */
export class PcmScheduler {
  private readonly leadSeconds: number;
  private readonly maxQueuedSeconds: number;
  private scheduledUntil = 0;
  private format: { sampleRate: number; channels: number } | null = null;
  private readonly scheduled = new Set<PcmSchedulerSource>();

  constructor(
    private readonly context: PcmSchedulerContext,
    options: PcmSchedulerOptions = {},
  ) {
    this.leadSeconds = options.leadSeconds ?? 0.05;
    this.maxQueuedSeconds = options.maxQueuedSeconds ?? 0.5;
  }

  enqueue(pcm: Float32Array, sampleRate: number, channels: number): boolean {
    if (!Number.isFinite(sampleRate) || sampleRate <= 0 || !Number.isInteger(channels) || channels <= 0) return false;
    if (pcm.length === 0 || pcm.length % channels !== 0) return false;

    if (!this.format || this.format.sampleRate !== sampleRate || this.format.channels !== channels) {
      this.reset();
      this.format = { sampleRate, channels };
    }

    const frames = pcm.length / channels;
    const duration = frames / sampleRate;
    const now = this.context.currentTime;
    const start = Math.max(now + this.leadSeconds, this.scheduledUntil);
    if (start + duration - now > this.maxQueuedSeconds + Number.EPSILON) return false;

    const buffer = this.context.createBuffer(channels, frames, sampleRate);
    for (let channel = 0; channel < channels; channel++) {
      const plane = buffer.getChannelData(channel);
      for (let frame = 0; frame < frames; frame++) plane[frame] = pcm[frame * channels + channel]!;
    }

    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    source.onended = () => this.scheduled.delete(source);
    source.start(start);
    this.scheduled.add(source);
    this.scheduledUntil = start + duration;
    return true;
  }

  reset(): void {
    for (const source of this.scheduled) {
      try {
        source.stop();
      } catch {
        // An already-ended source cannot be stopped again.
      }
      try {
        source.disconnect();
      } catch {
        // A source may already have been disconnected by the browser.
      }
    }
    this.scheduled.clear();
    this.scheduledUntil = 0;
    this.format = null;
  }
}
