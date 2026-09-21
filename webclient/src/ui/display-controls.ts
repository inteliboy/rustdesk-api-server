import type { DisplayInfo } from '../core/contracts';
import { SupportedDecoding_PreferCodec } from '../gen/message';

const SUPPORTED_VIRTUAL_DISPLAY_IDDS = new Set(['rustdesk_idd', 'amyuni_idd']);

export type Resolution = { width: number; height: number };

function isResolution(value: Resolution): boolean {
  return Number.isSafeInteger(value.width)
    && Number.isSafeInteger(value.height)
    && value.width > 0
    && value.height > 0
    && value.width <= 16384
    && value.height <= 16384;
}

export function canToggleVirtualDisplay(platform: string, platformAdditions: string): boolean {
  return parseVirtualDisplayCapability(platform, platformAdditions) !== null;
}

export type VirtualDisplayCapability = {
  impl: 'rustdesk_idd' | 'amyuni_idd';
  rustdeskIds: number[];
  amyuniCount: number;
};

function parseAdditionRecord(raw: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

export function mergePlatformAdditions(previous: string, incoming: string): string {
  const next = parseAdditionRecord(incoming);
  if (!next) return '';
  const prior = parseAdditionRecord(previous);
  for (const stableKey of ['is_installed', 'support_view_camera'] as const) {
    if (!(stableKey in next) && typeof prior?.[stableKey] === 'boolean') {
      next[stableKey] = prior[stableKey];
    }
  }
  if (!('idd_impl' in next) && typeof prior?.idd_impl === 'string' && SUPPORTED_VIRTUAL_DISPLAY_IDDS.has(prior.idd_impl)) {
    next.idd_impl = prior.idd_impl;
  }
  return JSON.stringify(next);
}

export function parseVirtualDisplayCapability(
  platform: string,
  platformAdditions: string,
): VirtualDisplayCapability | null {
  if (platform !== 'Windows') return null;
  try {
    const additions = JSON.parse(platformAdditions) as Record<string, unknown>;
    const impl = additions.idd_impl;
    if (
      additions.is_installed !== true ||
      typeof impl !== 'string' ||
      !SUPPORTED_VIRTUAL_DISPLAY_IDDS.has(impl)
    ) return null;
    const idsValue = additions.virtual_displays ?? additions.rustdesk_virtual_displays;
    const rustdeskIds = Array.isArray(idsValue)
      ? [...new Set(idsValue.filter((value): value is number => Number.isSafeInteger(value) && value > 0 && value <= 4))].sort()
      : [];
    const rawCount = additions.amyuni_virtual_displays ?? additions.virtual_display_count ?? additions.amyuni_virtual_display_count;
    const amyuniCount = Number.isSafeInteger(rawCount) ? Math.max(0, Math.min(4, Number(rawCount))) : 0;
    return { impl: impl as VirtualDisplayCapability['impl'], rustdeskIds, amyuniCount };
  } catch {
    return null;
  }
}

export function adaptFps(input: {
  target: number;
  droppedDelta: number;
  stableSamples: number;
  cap: number;
}): { target: number; stableSamples: number } {
  const target = Math.max(5, Math.min(120, Math.round(input.target)));
  const cap = Math.max(5, Math.min(120, Math.round(input.cap)));
  if (input.droppedDelta > 0) return { target: Math.max(5, target - 10), stableSamples: 0 };
  const stableSamples = input.stableSamples + 1;
  if (stableSamples >= 5 && target < cap) return { target: Math.min(cap, target + 5), stableSamples: 0 };
  return { target, stableSamples };
}

export function mergeDisplayRefresh(
  existing: DisplayInfo[],
  incoming: DisplayInfo[],
  activeIndex: number,
): DisplayInfo[] {
  const active = existing[activeIndex];
  if (!active) return incoming;
  const refreshed = incoming[activeIndex];
  if (!refreshed) return incoming;
  const next = [...incoming];
  next[activeIndex] = {
    ...(refreshed ?? active),
    index: active.index,
    x: active.x,
    y: active.y,
    width: active.width,
    height: active.height,
    scale: active.scale,
    online: refreshed?.online ?? false,
    cursorEmbedded: active.cursorEmbedded,
    originalResolution: active.originalResolution,
    resolutions: active.resolutions,
  };
  return next;
}

export function canUseRemoteCursor(
  platform: string,
  display: { cursorEmbedded: boolean; online?: boolean } | undefined,
): boolean {
  const mobile = platform.toLowerCase();
  return !!display && display.online !== false && !display.cursorEmbedded && mobile !== 'android' && mobile !== 'ios';
}

export function mapRemoteCursorToCanvas(
  point: { x: number; y: number },
  display: { x: number; y: number; width: number; height: number; scale?: number },
  canvas: { left: number; top: number; width: number; height: number },
): { x: number; y: number } | null {
  const candidateScale = display.scale ?? 1;
  const scale = Number.isFinite(candidateScale) && candidateScale > 0 ? candidateScale : 1;
  const localX = (point.x - display.x) * scale;
  const localY = (point.y - display.y) * scale;
  if (localX < 0 || localY < 0 || localX >= display.width || localY >= display.height) return null;
  if (display.width <= 0 || display.height <= 0 || canvas.width <= 0 || canvas.height <= 0) return null;
  return {
    x: canvas.left + (localX / display.width) * canvas.width,
    y: canvas.top + (localY / display.height) * canvas.height,
  };
}

export type ResolutionChoice = Resolution & { label: string };

export function buildResolutionChoices(input: {
  supported: Resolution[];
  original?: Resolution;
  fit?: Resolution;
  isVirtual: boolean;
}): ResolutionChoice[] {
  const values = [...input.supported];
  if (input.original && isResolution(input.original)) values.push(input.original);
  if (input.fit && isResolution(input.fit) && (input.isVirtual || input.supported.some((r) => r.width === input.fit!.width && r.height === input.fit!.height))) {
    values.push(input.fit);
  }
  const unique = new Map<string, Resolution>();
  for (const value of values) if (isResolution(value)) unique.set(`${value.width}x${value.height}`, value);
  return [...unique.values()]
    .sort((a, b) => a.width * a.height - b.width * b.height)
    .map((value) => ({
      ...value,
      label: `${value.width}×${value.height}${input.original?.width === value.width && input.original.height === value.height ? ' (original)' : ''}`,
    }));
}

export function codecPreferenceValue(codec: string): SupportedDecoding_PreferCodec | null {
  return ({
    auto: SupportedDecoding_PreferCodec.Auto,
    vp9: SupportedDecoding_PreferCodec.VP9,
    h264: SupportedDecoding_PreferCodec.H264,
    h265: SupportedDecoding_PreferCodec.H265,
    vp8: SupportedDecoding_PreferCodec.VP8,
    av1: SupportedDecoding_PreferCodec.AV1,
  } as Record<string, SupportedDecoding_PreferCodec>)[codec] ?? null;
}

/** Native peers only accept physical modes they advertised; virtual displays accept a valid local size. */
export function bestFitResolution(
  width: number,
  height: number,
  supported: Resolution[],
  isVirtualDisplay: boolean,
): Resolution | null {
  const fit = { width: Math.round(width), height: Math.round(height) };
  if (!isResolution(fit)) return null;
  if (isVirtualDisplay) return fit;
  return supported.find((resolution) => resolution.width === fit.width && resolution.height === fit.height) ?? null;
}
