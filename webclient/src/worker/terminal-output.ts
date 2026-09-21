export const MAX_TERMINAL_FRAME_BYTES = 1_000_000;

type ZstdDecode = (data: Uint8Array, uncompressedSize: number) => Uint8Array;

/**
 * Bounds both attacker-controlled compressed input and the WASM decoder output
 * allocation. zstddec receives the explicit capacity instead of trusting the
 * frame-declared decompressed size.
 */
export function decodeTerminalOutput(
  data: Uint8Array,
  compressed: boolean,
  decode: ZstdDecode,
  maxBytes = MAX_TERMINAL_FRAME_BYTES,
): Uint8Array {
  if (data.byteLength > maxBytes) {
    throw new Error(`Terminal output frame exceeds ${maxBytes} bytes`);
  }
  if (!compressed) return data;
  const output = decode(data, maxBytes);
  if (output.byteLength > maxBytes) {
    throw new Error(`Decompressed terminal output exceeds ${maxBytes} bytes`);
  }
  return output;
}
