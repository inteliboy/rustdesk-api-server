// Opus playback via WebCodecs AudioDecoder. Decoded PCM is handed up through
// onPcm (interleaved f32) — the worker posts it to the main thread, which
// feeds an AudioWorklet ring buffer.
// TODO(safari): AudioDecoder is unavailable there — add a libopus WASM
// fallback decode path. Until then audio is silently off on Safari.
export class AudioPipeline {
  onPcm: ((pcm: Float32Array, sampleRate: number, channels: number) => void) | null = null;

  private decoder: AudioDecoder | null = null;
  private tsUs = 0;

  setFormat(sampleRate: number, channels: number): void {
    this.teardown();
    if (typeof AudioDecoder === 'undefined') return; // Safari: see TODO above
    try {
      const dec = new AudioDecoder({
        output: (data) => this.handleOutput(data),
        error: () => this.teardown(),
      });
      dec.configure({ codec: 'opus', sampleRate, numberOfChannels: channels });
      this.decoder = dec;
      this.tsUs = 0;
    } catch {
      this.decoder = null;
    }
  }

  pushFrame(data: Uint8Array): void {
    const dec = this.decoder;
    if (!dec || dec.state !== 'configured') return;
    try {
      dec.decode(new EncodedAudioChunk({ type: 'key', timestamp: this.tsUs, data }));
      this.tsUs += 10_000; // nominal 10ms packets; decoder only needs monotonic timestamps
    } catch {
      this.teardown();
    }
  }

  close(): void {
    this.teardown();
  }

  private handleOutput(data: AudioData): void {
    try {
      const frames = data.numberOfFrames;
      const chs = data.numberOfChannels;
      if (frames === 0 || chs === 0) return;
      const out = new Float32Array(frames * chs);
      const plane = new Float32Array(frames);
      for (let c = 0; c < chs; c++) {
        data.copyTo(plane, { planeIndex: c, format: 'f32-planar' });
        for (let i = 0; i < frames; i++) out[i * chs + c] = plane[i]!;
      }
      this.onPcm?.(out, data.sampleRate, chs);
    } finally {
      data.close();
    }
  }

  private teardown(): void {
    const dec = this.decoder;
    this.decoder = null;
    if (dec && dec.state !== 'closed') {
      try {
        dec.close();
      } catch {
        // already closed by an error callback
      }
    }
  }
}
