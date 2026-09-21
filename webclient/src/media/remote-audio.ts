import { PcmScheduler, type PcmSchedulerContext } from './pcm-scheduler';

export interface RemoteAudioNode {
  connect(destination: unknown): void;
  disconnect(destination?: unknown): void;
}

export interface RemoteAudioGainNode extends RemoteAudioNode {
  gain: { value: number };
}

export interface RemoteAudioContext extends PcmSchedulerContext {
  state: AudioContextState;
  resume(): Promise<void>;
  close?(): Promise<void>;
  createGain(): RemoteAudioGainNode;
  createMediaStreamDestination(): RemoteAudioNode & { stream: MediaStream };
}

export type RemoteAudioRecordingTap = { stream: MediaStream; close(): void };

/** Main-thread playback state. AudioContext creation stays at the UI boundary. */
export class RemoteAudioPlayback {
  private readonly sourceBus: RemoteAudioGainNode;
  private readonly output: RemoteAudioGainNode;
  private readonly scheduler: PcmScheduler;
  private volume = 1;
  private muted = false;

  constructor(private readonly context: RemoteAudioContext) {
    this.sourceBus = context.createGain();
    this.output = context.createGain();
    this.sourceBus.connect(this.output);
    this.output.connect(context.destination);
    const sourceBus = this.sourceBus;
    this.scheduler = new PcmScheduler({
      get currentTime(): number { return context.currentTime; },
      get destination(): unknown { return sourceBus; },
      createBuffer: context.createBuffer.bind(context),
      createBufferSource: context.createBufferSource.bind(context),
    });
  }

  enqueue(pcm: Float32Array, sampleRate: number, channels: number): boolean {
    return this.scheduler.enqueue(pcm, sampleRate, channels);
  }

  createRecordingTap(): RemoteAudioRecordingTap {
    const destination = this.context.createMediaStreamDestination();
    this.sourceBus.connect(destination);
    let closed = false;
    return {
      stream: destination.stream,
      close: () => {
        if (closed) return;
        closed = true;
        try { this.sourceBus.disconnect(destination); } catch { /* already disconnected */ }
        try { destination.disconnect(); } catch { /* already disconnected */ }
      },
    };
  }

  reset(): void {
    this.scheduler.reset();
  }

  close(): void {
    this.scheduler.reset();
    this.sourceBus.disconnect();
    this.output.disconnect();
    if (this.context.close) void this.context.close().catch(() => {});
  }

  setVolume(volume: number): void {
    this.volume = Math.max(0, Math.min(1, Number.isFinite(volume) ? volume : 1));
    this.applyVolume();
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.applyVolume();
  }

  private applyVolume(): void {
    this.output.gain.value = this.muted ? 0 : this.volume;
  }

  async resumeFromUserGesture(): Promise<boolean> {
    if (this.context.state === 'running') return true;
    try {
      await this.context.resume();
      return true;
    } catch {
      return false;
    }
  }
}
