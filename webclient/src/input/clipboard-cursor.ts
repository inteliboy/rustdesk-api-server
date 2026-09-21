import { ZSTDDecoder } from 'zstddec';
import { ClipboardFormat } from '../gen/message';
import type { Clipboard, CursorData } from '../gen/message';

let decoder: ZSTDDecoder | undefined;
let decoderReady = false;

// zstddec wasm must be instantiated once before any sync decode.
export async function initZstd(): Promise<void> {
  if (!decoder) decoder = new ZSTDDecoder();
  if (!decoderReady) {
    await decoder.init();
    decoderReady = true;
  }
}

// RustDesk peers compress with zstd's bulk API, which embeds the content size
// in the frame header — decode(data, 0) recovers it via ZSTD_findDecompressedSize.
export function zstdDecode(data: Uint8Array, uncompressedSize = 0): Uint8Array {
  if (!decoder || !decoderReady) throw new Error('zstd decoder not initialized: await initZstd() first');
  return decoder.decode(data, uncompressedSize);
}

function toBase64(bytes: Uint8Array): string {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

export async function cursorToDataUrl(
  c: CursorData,
): Promise<{ pngDataUrl: string; hotx: number; hoty: number }> {
  if (typeof OffscreenCanvas === 'undefined') {
    throw new Error('cursorToDataUrl requires OffscreenCanvas (run in a worker or browser)');
  }
  await initZstd();
  const expected = c.width * c.height * 4;
  const rgba = zstdDecode(c.colors, expected); // straight RGBA rows
  if (rgba.length < expected) {
    throw new Error(`cursor decompress: got ${rgba.length} bytes, expected ${expected}`);
  }
  const canvas = new OffscreenCanvas(c.width, c.height);
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('cursorToDataUrl: 2d context unavailable');
  const px = new Uint8ClampedArray(expected);
  px.set(rgba.subarray(0, expected));
  ctx.putImageData(new ImageData(px, c.width, c.height), 0, 0);
  const blob = await canvas.convertToBlob({ type: 'image/png' });
  const png = new Uint8Array(await blob.arrayBuffer());
  return { pngDataUrl: `data:image/png;base64,${toBase64(png)}`, hotx: c.hotx, hoty: c.hoty };
}

export function decodeClipboardText(c: Clipboard): string | null {
  if (c.format !== ClipboardFormat.Text) return null;
  const bytes = c.compress ? zstdDecode(c.content) : c.content;
  return new TextDecoder().decode(bytes);
}

export async function readLocalClipboardText(): Promise<string | null> {
  if (typeof navigator === 'undefined' || typeof navigator.clipboard?.readText !== 'function') return null;
  try {
    return await navigator.clipboard.readText();
  } catch {
    return null; // permission denied / not focused
  }
}
