// Shared UI helpers for the desktop client (app.ts) and the file-transfer
// window (file-app.ts): icons, state labels, saved-credential storage, session
// config plumbing. app.ts re-exports everything here for back-compat.
import type { DisplayInfo, SessionConfig, SessionState, UiCommand } from '../core/contracts';
import type { DisplayRect } from '../input/mouse-keyboard';

export type RdGlobalConfig = {
  peerId?: string;
  serverKeyB64: string;
  wsIdUrl: string;
  wsRelayUrl: string;
  myId: string;
  myName: string;
  /** Console version, injected per request — see OVERLAY_VERSION. */
  version?: string;
  workerUrl?: string;
  /** Where this client's source is (AGPL-3.0), shown in the session's Details panel. */
  sourceUrl?: string;
};

export const QUALITY = { best: 4, balanced: 3, speed: 2 } as const; // ImageQuality enum values

/**
 * Fallback only. The real version is injected per request by the console
 * (window.__RD__.version) and read via overlayVersion() below.
 *
 * The web client is not released independently — it only ships inside a
 * server release — so it carries the project version rather than one of
 * its own. Taking it at runtime matters: the bundle is rebuilt only when
 * webclient/src changes, so a version baked in at build time would still claim
 * the older release on every release that did not touch the client.
 */
export const OVERLAY_VERSION = 'dev';

export function overlayVersion(cfg?: { version?: string } | null): string {
  const v = cfg?.version?.trim();
  return v ? `v${v}` : OVERLAY_VERSION;
}

export const STATE_LABEL: Record<SessionState, string> = {
  connecting: 'Connecting',
  rendezvous: 'Contacting server',
  relay: 'Opening relay',
  handshake: 'Securing channel',
  login: 'Authenticating',
  streaming: 'Connected',
  error: 'Error',
  closed: 'Disconnected',
  needAccept: 'Waiting for remote user to accept',
};

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatMbps(mbps: number): string {
  if (!Number.isFinite(mbps) || mbps < 0) return '0.00 Mbps';
  const n = mbps >= 100 ? mbps.toFixed(0) : mbps >= 10 ? mbps.toFixed(1) : mbps.toFixed(2);
  return `${n} Mbps`;
}

/**
 * Strip whitespace from a device ID.
 *
 * The RustDesk client displays IDs grouped as "123 456 789", so that is what
 * people copy and type. The spaces are presentation only — the ID is
 * "123456789" — and an ID with them in it simply is not found.
 *
 * Whitespace only, deliberately: IDs are not always numeric (a device can be
 * given a custom alphanumeric ID), so stripping anything broader would break
 * legitimate IDs. \s covers the non-breaking space that copying from a
 * rendered UI can produce.
 */
export function normalizePeerId(raw: string): string {
  return raw.replace(/\s+/g, '');
}

export function peerIdFromSearch(search: string): string | null {
  const id = normalizePeerId(new URLSearchParams(search).get('id') ?? '');
  return id ? id : null;
}

// ?lo=1 marks "the user logged out on purpose" — it suppresses saved-password
// auto-login until the next explicit connect. Set by the disconnect button so
// a reload after logging out stays on the connect screen.
export function loggedOutFromSearch(search: string): boolean {
  return new URLSearchParams(search).get('lo') === '1';
}

export function resolveWorkerUrl(
  cfgUrl: string | undefined,
  attrUrl: string | null | undefined,
  baseUrl: string,
): string {
  if (cfgUrl) return cfgUrl;
  if (attrUrl) return attrUrl;
  return new URL('session.worker.js', baseUrl).href;
}

export function cursorCss(pngDataUrl: string, hotx: number, hoty: number): string {
  const x = Math.max(0, Math.round(hotx));
  const y = Math.max(0, Math.round(hoty));
  return `url("${pngDataUrl}") ${x} ${y}, auto`;
}

// RustDesk API Server: saving a device password is switched off. The hash the
// upstream client kept here (SHA256(pw||salt)) logs in to the device exactly like
// the password does, and this project's rule is that device credentials never
// go into browser storage. The functions stay so the call sites are unchanged:
// nothing is loaded, nothing is saved, and clearSavedHash still removes what an
// earlier version of the client may have left behind.
const SAVED_PW_PREFIX = 'rustdesk-api.rdpw.';
const LEGACY_SAVED_PW_PREFIX = 'cortendesk.rdpw.';
export function loadSavedHash(_peerId: string): string | null {
  return null;
}
export function saveSavedHash(_peerId: string, _hashHex: string): void {
  /* deliberately does nothing — see above */
}
export function clearSavedHash(peerId: string): void {
  try {
    localStorage.removeItem(SAVED_PW_PREFIX + peerId);
    localStorage.removeItem(LEGACY_SAVED_PW_PREFIX + peerId);
  } catch {
    /* non-fatal */
  }
}

export function buildSessionConfig(
  g: RdGlobalConfig,
  peerId: string,
  password: string,
  savedHashHex?: string,
  connType?: SessionConfig['connType'],
): SessionConfig {
  return {
    peerId,
    serverKeyB64: g.serverKeyB64,
    wsIdUrl: g.wsIdUrl,
    wsRelayUrl: g.wsRelayUrl,
    password,
    myId: g.myId,
    myName: g.myName,
    savedHashHex,
    ...(connType ? { connType } : {}),
  };
}

export function displayToRect(d: DisplayInfo): DisplayRect {
  return { x: d.x, y: d.y, width: d.width, height: d.height };
}

export function placePopover(
  anchor: { left: number; top: number; bottom: number; width: number },
  popover: { width: number; height: number },
  viewport: { width: number; height: number },
  preferAbove = false,
): { left: number; top: number; maxHeight: number } {
  const margin = 8;
  const gap = 10;
  const belowTop = anchor.bottom + gap;
  const aboveBottom = anchor.top - gap;
  const belowSpace = Math.max(0, viewport.height - margin - belowTop);
  const aboveSpace = Math.max(0, aboveBottom - margin);
  const preferredSpace = preferAbove ? aboveSpace : belowSpace;
  const alternateSpace = preferAbove ? belowSpace : aboveSpace;
  const openAbove = popover.height > preferredSpace && alternateSpace > preferredSpace
    ? !preferAbove
    : preferAbove;
  const maxHeight = Math.max(0, Math.floor(openAbove ? aboveSpace : belowSpace));
  const visibleHeight = Math.min(Math.max(0, popover.height), maxHeight);
  const rawTop = openAbove ? aboveBottom - visibleHeight : belowTop;
  const maxTop = Math.max(margin, viewport.height - margin - visibleHeight);
  const top = Math.max(margin, Math.min(rawTop, maxTop));
  const centeredLeft = anchor.left + anchor.width / 2 - popover.width / 2;
  const maxLeft = Math.max(margin, viewport.width - popover.width - margin);
  const left = Math.max(margin, Math.min(centeredLeft, maxLeft));
  return { left: Math.round(left), top: Math.round(top), maxHeight };
}

/**
 * Is diagnostic logging on?
 *
 * Enabled by `?debug=1` or by setting `window.__rdDebug = true` at runtime, so
 * a session already in progress can be instrumented without a reload — which
 * matters when the thing being diagnosed only happens once you are connected.
 *
 * Off by default: these logs fire per input event, and a remote session
 * generates a lot of those.
 */
export function debugEnabled(search: string, win: unknown): boolean {
  if ((win as { __rdDebug?: unknown } | undefined)?.__rdDebug === true) return true;
  const v = new URLSearchParams(search).get('debug');
  return v === '1' || v === 'true';
}

/**
 * Fold the host's Misc.switch_display into the display list, in place.
 *
 * The host's geometry wins over the PeerInfo snapshot: PeerInfo is taken at
 * login and can be stale by the time anyone switches monitors (resolution
 * changed, displays re-arranged, a monitor that was offline then). Since input
 * coordinates are absolute virtual-desktop positions, a stale origin sends
 * every click to the wrong monitor.
 *
 * Zero width/height mean "unchanged" rather than "collapse this display" —
 * writing them through would leave the coordinate mapping dividing by a size
 * the capture is not actually using.
 */
export function applySwitchDisplay(
  displays: DisplayInfo[],
  ev: {
    index: number; x: number; y: number; width: number; height: number;
    cursorEmbedded?: boolean;
    originalResolution?: { width: number; height: number };
    resolutions?: Array<{ width: number; height: number }>;
  },
): void {
  const d = displays[ev.index];
  if (!d) return;
  d.x = ev.x;
  d.y = ev.y;
  if (ev.width > 0) d.width = ev.width;
  if (ev.height > 0) d.height = ev.height;
  if (ev.cursorEmbedded !== undefined) d.cursorEmbedded = ev.cursorEmbedded;
  if (ev.originalResolution) d.originalResolution = ev.originalResolution;
  if (ev.resolutions) d.resolutions = ev.resolutions;
}

export type IconName =
  | 'pointer'
  | 'touch'
  | 'typeText'
  | 'chat'
  | 'more'
  | 'check'
  | 'chevronDown'
  | 'info'
  | 'send'
  | 'fullscreen'
  | 'fullscreenExit'
  | 'monitor'
  | 'keyboard'
  | 'refresh'
  | 'clipboard'
  | 'pulse'
  | 'power'
  | 'folderTransfer'
  | 'folder'
  | 'folderOpen'
  | 'file'
  | 'drive'
  | 'arrowUp'
  | 'home'
  | 'newFolder'
  | 'trash'
  | 'rename'
  | 'sendRight'
  | 'sendLeft'
  | 'fileUpload'
  | 'eye'
  | 'eyeOff'
  | 'close';

export const ICONS: Record<IconName, string> = {
  pointer: 'M15.3873 13.4975L17.9403 20.5117L13.2418 22.2218L10.6889 15.2076L6.79004 17.6529L8.4086 1.63318L19.9457 12.8646L15.3873 13.4975ZM15.3768 19.3163L12.6618 11.8568L15.6212 11.4459L9.98201 5.9561L9.19088 13.7863L11.7221 12.1988L14.4371 19.6583L15.3768 19.3163Z', // cursor-line
  touch: 'M12.5002 2C12.2241 2 12.0002 2.22386 12.0002 2.5V12H10.0002V4.5C10.0002 4.22386 9.77634 4 9.5002 4C9.22405 4 9.0002 4.22386 9.0002 4.5V14C8.64653 14 7.00024 14 7.00024 14C6.61911 12.3792 5.64236 11.4407 4.5954 11.3216C4.87926 12.0664 5.36117 13.2592 6.16634 15.0995C7.02511 17.0622 7.89128 18.5218 9.00374 19.4986C10.0783 20.442 11.4586 21 13.5002 21C16.5378 21 19.0002 18.5377 19.0002 15.5002V7C19.0002 6.72386 18.7763 6.5 18.5002 6.5C18.2241 6.5 18.0002 6.72386 18.0002 7V12H16.0002V4C16.0002 3.72386 15.7763 3.5 15.5002 3.5C15.2241 3.5 15.0002 3.72386 15.0002 4V12H13.0002V2.5C13.0002 2.22386 12.7763 2 12.5002 2ZM21.0002 15.5002C21.0002 19.6424 17.6423 23 13.5002 23C11.0417 23 9.17214 22.308 7.68416 21.0015C6.23411 19.7283 5.22528 17.9381 4.33405 15.9012C3.40393 13.7753 2.89004 12.4804 2.60991 11.7235C2.25318 10.7597 2.74616 9.41212 4.08583 9.31846C5.24076 9.23771 6.22061 9.61249 7.0002 10.2587V4.5C7.0002 3.11929 8.11949 2 9.5002 2C9.68522 2 9.86554 2.0201 10.0391 2.05823C10.2477 0.888227 11.2702 0 12.5002 0C13.5602 0 14.4661 0.659694 14.8298 1.59091C15.0431 1.53167 15.268 1.5 15.5002 1.5C16.8809 1.5 18.0002 2.61929 18.0002 4V4.55001C18.1618 4.51722 18.329 4.5 18.5002 4.5C19.8809 4.5 21.0002 5.61929 21.0002 7V15.5002Z', // hand
  typeText: 'M13 6V21H11V6H5V4H19V6H13Z', // text
  chat: 'M7.29117 20.8242L2 22L3.17581 16.7088C2.42544 15.3056 2 13.7025 2 12C2 6.47715 6.47715 2 12 2C17.5228 2 22 6.47715 22 12C22 17.5228 17.5228 22 12 22C10.2975 22 8.6944 21.5746 7.29117 20.8242ZM7.58075 18.711L8.23428 19.0605C9.38248 19.6745 10.6655 20 12 20C16.4183 20 20 16.4183 20 12C20 7.58172 16.4183 4 12 4C7.58172 4 4 7.58172 4 12C4 13.3345 4.32549 14.6175 4.93949 15.7657L5.28896 16.4192L4.63416 19.3658L7.58075 18.711Z', // chat-3-line
  more: 'M4.5 10.5C3.675 10.5 3 11.175 3 12C3 12.825 3.675 13.5 4.5 13.5C5.325 13.5 6 12.825 6 12C6 11.175 5.325 10.5 4.5 10.5ZM19.5 10.5C18.675 10.5 18 11.175 18 12C18 12.825 18.675 13.5 19.5 13.5C20.325 13.5 21 12.825 21 12C21 11.175 20.325 10.5 19.5 10.5ZM12 10.5C11.175 10.5 10.5 11.175 10.5 12C10.5 12.825 11.175 13.5 12 13.5C12.825 13.5 13.5 12.825 13.5 12C13.5 11.175 12.825 10.5 12 10.5Z', // more-line
  check: 'M9.9997 15.1709L19.1921 5.97852L20.6063 7.39273L9.9997 17.9993L3.63574 11.6354L5.04996 10.2212L9.9997 15.1709Z', // check-line
  chevronDown: 'M11.9999 13.1714L16.9497 8.22168L18.3639 9.63589L11.9999 15.9999L5.63599 9.63589L7.0502 8.22168L11.9999 13.1714Z', // arrow-down-s-line
  info: 'M12 22C6.47715 22 2 17.5228 2 12C2 6.47715 6.47715 2 12 2C17.5228 2 22 6.47715 22 12C22 17.5228 17.5228 22 12 22ZM12 20C16.4183 20 20 16.4183 20 12C20 7.58172 16.4183 4 12 4C7.58172 4 4 7.58172 4 12C4 16.4183 7.58172 20 12 20ZM11 7H13V9H11V7ZM11 11H13V17H11V11Z', // information-line
  send: 'M21.7267 2.95694L16.2734 22.0432C16.1225 22.5716 15.7979 22.5956 15.5563 22.1126L11 13L1.9229 9.36919C1.41322 9.16532 1.41953 8.86022 1.95695 8.68108L21.0432 2.31901C21.5716 2.14285 21.8747 2.43866 21.7267 2.95694ZM19.0353 5.09647L6.81221 9.17085L12.4488 11.4255L15.4895 17.5068L19.0353 5.09647Z', // send-plane-line
  fullscreen: 'M8 3V5H4V9H2V3H8ZM2 21V15H4V19H8V21H2ZM22 21H16V19H20V15H22V21ZM22 9H20V5H16V3H22V9Z', // fullscreen-line
  fullscreenExit: 'M18 7H22V9H16V3H18V7ZM8 9H2V7H6V3H8V9ZM18 17V21H16V15H22V17H18ZM8 15V21H6V17H2V15H8Z', // fullscreen-exit-line
  monitor: 'M4 16H20V5H4V16ZM13 18V20H17V22H7V20H11V18H2.9918C2.44405 18 2 17.5511 2 16.9925V4.00748C2 3.45107 2.45531 3 2.9918 3H21.0082C21.556 3 22 3.44892 22 4.00748V16.9925C22 17.5489 21.5447 18 21.0082 18H13Z', // computer-line
  keyboard: 'M4 5V19H20V5H4ZM3 3H21C21.5523 3 22 3.44772 22 4V20C22 20.5523 21.5523 21 21 21H3C2.44772 21 2 20.5523 2 20V4C2 3.44772 2.44772 3 3 3ZM6 7H8V9H6V7ZM6 11H8V13H6V11ZM6 15H18V17H6V15ZM11 11H13V13H11V11ZM11 7H13V9H11V7ZM16 7H18V9H16V7ZM16 11H18V13H16V11Z', // keyboard-box-line
  refresh: 'M5.46257 4.43262C7.21556 2.91688 9.5007 2 12 2C17.5228 2 22 6.47715 22 12C22 14.1361 21.3302 16.1158 20.1892 17.7406L17 12H20C20 7.58172 16.4183 4 12 4C9.84982 4 7.89777 4.84827 6.46023 6.22842L5.46257 4.43262ZM18.5374 19.5674C16.7844 21.0831 14.4993 22 12 22C6.47715 22 2 17.5228 2 12C2 9.86386 2.66979 7.88416 3.8108 6.25944L7 12H4C4 16.4183 7.58172 20 12 20C14.1502 20 16.1022 19.1517 17.5398 17.7716L18.5374 19.5674Z', // refresh-line
  clipboard: 'M7 4V2H17V4H20.0066C20.5552 4 21 4.44495 21 4.9934V21.0066C21 21.5552 20.5551 22 20.0066 22H3.9934C3.44476 22 3 21.5551 3 21.0066V4.9934C3 4.44476 3.44495 4 3.9934 4H7ZM7 6H5V20H19V6H17V8H7V6ZM9 4V6H15V4H9Z', // clipboard-line
  pulse: 'M9 7.53861L15 21.5386L18.6594 13H23V11H17.3406L15 16.4614L9 2.46143L5.3406 11H1V13H6.6594L9 7.53861Z', // pulse-line
  power: 'M6.26489 3.80698L7.41191 5.44558C5.34875 6.89247 4 9.28873 4 12C4 16.4183 7.58172 20 12 20C16.4183 20 20 16.4183 20 12C20 9.28873 18.6512 6.89247 16.5881 5.44558L17.7351 3.80698C20.3141 5.61559 22 8.61091 22 12C22 17.5228 17.5228 22 12 22C6.47715 22 2 17.5228 2 12C2 8.61091 3.68594 5.61559 6.26489 3.80698ZM11 12V2H13V12H11Z', // shut-down-line
  folderTransfer: 'M12.4142 5H21C21.5523 5 22 5.44772 22 6V20C22 20.5523 21.5523 21 21 21H3C2.44772 21 2 20.5523 2 20V4C2 3.44772 2.44772 3 3 3H10.4142L12.4142 5ZM4 5V19H20V7H11.5858L9.58579 5H4ZM12 12V9L16 13L12 17V14H8V12H12Z', // folder-transfer-line
  folder: 'M12.4142 5H21C21.5523 5 22 5.44772 22 6V20C22 20.5523 21.5523 21 21 21H3C2.44772 21 2 20.5523 2 20V4C2 3.44772 2.44772 3 3 3H10.4142L12.4142 5ZM4 7V19H20V7H4Z', // folder-3-line
  folderOpen: 'M3 21C2.44772 21 2 20.5523 2 20V4C2 3.44772 2.44772 3 3 3H10.4142L12.4142 5H20C20.5523 5 21 5.44772 21 6V9H19V7H11.5858L9.58579 5H4V16.998L5.5 11H22.5L20.1894 20.2425C20.0781 20.6877 19.6781 21 19.2192 21H3ZM19.9384 13H7.06155L5.56155 19H18.4384L19.9384 13Z', // folder-open-line
  file: 'M9 2.00318V2H19.9978C20.5513 2 21 2.45531 21 2.9918V21.0082C21 21.556 20.5551 22 20.0066 22H3.9934C3.44476 22 3 21.5501 3 20.9932V8L9 2.00318ZM5.82918 8H9V4.83086L5.82918 8ZM11 4V9C11 9.55228 10.5523 10 10 10H5V20H19V4H11Z', // file-line
  drive: 'M5 14H19V4H5V14ZM5 16V20H19V16H5ZM4 2H20C20.5523 2 21 2.44772 21 3V21C21 21.5523 20.5523 22 20 22H4C3.44772 22 3 21.5523 3 21V3C3 2.44772 3.44772 2 4 2ZM15 17H17V19H15V17Z', // hard-drive-2-line
  arrowUp: 'M13.0001 7.82843V20H11.0001V7.82843L5.63614 13.1924L4.22192 11.7782L12.0001 4L19.7783 11.7782L18.3641 13.1924L13.0001 7.82843Z', // arrow-up-line
  home: 'M19 21H5C4.44772 21 4 20.5523 4 20V11L1 11L11.3273 1.6115C11.7087 1.26475 12.2913 1.26475 12.6727 1.6115L23 11L20 11V20C20 20.5523 19.5523 21 19 21ZM13 19H18V9.15745L12 3.7029L6 9.15745V19H11V13H13V19Z', // home-4-line
  newFolder: 'M12.4142 5H21C21.5523 5 22 5.44772 22 6V20C22 20.5523 21.5523 21 21 21H3C2.44772 21 2 20.5523 2 20V4C2 3.44772 2.44772 3 3 3H10.4142L12.4142 5ZM4 5V19H20V7H11.5858L9.58579 5H4ZM11 12V9H13V12H16V14H13V17H11V14H8V12H11Z', // folder-add-line
  trash: 'M17 6H22V8H20V21C20 21.5523 19.5523 22 19 22H5C4.44772 22 4 21.5523 4 21V8H2V6H7V3C7 2.44772 7.44772 2 8 2H16C16.5523 2 17 2.44772 17 3V6ZM18 8H6V20H18V8ZM9 11H11V17H9V11ZM13 11H15V17H13V11ZM9 4V6H15V4H9Z', // delete-bin-line
  rename: 'M6.41421 15.89L16.5563 5.74785L15.1421 4.33363L5 14.4758V15.89H6.41421ZM7.24264 17.89H3V13.6473L14.435 2.21231C14.8256 1.82179 15.4587 1.82179 15.8492 2.21231L18.6777 5.04074C19.0682 5.43126 19.0682 6.06443 18.6777 6.45495L7.24264 17.89ZM3 19.89H21V21.89H3V19.89Z', // edit-line
  sendRight: 'M16.1716 10.9999L10.8076 5.63589L12.2218 4.22168L20 11.9999L12.2218 19.778L10.8076 18.3638L16.1716 12.9999H4V10.9999H16.1716Z', // arrow-right-line
  sendLeft: 'M7.82843 10.9999H20V12.9999H7.82843L13.1924 18.3638L11.7782 19.778L4 11.9999L11.7782 4.22168L13.1924 5.63589L7.82843 10.9999Z', // arrow-left-line
  fileUpload: 'M15 4H5V20H19V8H15V4ZM3 2.9918C3 2.44405 3.44749 2 3.9985 2H16L20.9997 7L21 20.9925C21 21.5489 20.5551 22 20.0066 22H3.9934C3.44476 22 3 21.5447 3 21.0082V2.9918ZM13 12V16H11V12H8L12 8L16 12H13Z', // file-upload-line
  eye: 'M12.0003 3C17.3924 3 21.8784 6.87976 22.8189 12C21.8784 17.1202 17.3924 21 12.0003 21C6.60812 21 2.12215 17.1202 1.18164 12C2.12215 6.87976 6.60812 3 12.0003 3ZM12.0003 19C16.2359 19 19.8603 16.052 20.7777 12C19.8603 7.94803 16.2359 5 12.0003 5C7.7646 5 4.14022 7.94803 3.22278 12C4.14022 16.052 7.7646 19 12.0003 19ZM12.0003 16.5C9.51498 16.5 7.50026 14.4853 7.50026 12C7.50026 9.51472 9.51498 7.5 12.0003 7.5C14.4855 7.5 16.5003 9.51472 16.5003 12C16.5003 14.4853 14.4855 16.5 12.0003 16.5ZM12.0003 14.5C13.381 14.5 14.5003 13.3807 14.5003 12C14.5003 10.6193 13.381 9.5 12.0003 9.5C10.6196 9.5 9.50026 10.6193 9.50026 12C9.50026 13.3807 10.6196 14.5 12.0003 14.5Z', // eye-line
  eyeOff: 'M17.8827 19.2968C16.1814 20.3755 14.1638 21.0002 12.0003 21.0002C6.60812 21.0002 2.12215 17.1204 1.18164 12.0002C1.61832 9.62282 2.81932 7.5129 4.52047 5.93457L1.39366 2.80777L2.80788 1.39355L22.6069 21.1925L21.1927 22.6068L17.8827 19.2968ZM5.9356 7.3497C4.60673 8.56015 3.6378 10.1672 3.22278 12.0002C4.14022 16.0521 7.7646 19.0002 12.0003 19.0002C13.5997 19.0002 15.112 18.5798 16.4243 17.8384L14.396 15.8101C13.7023 16.2472 12.8808 16.5002 12.0003 16.5002C9.51498 16.5002 7.50026 14.4854 7.50026 12.0002C7.50026 11.1196 7.75317 10.2981 8.19031 9.60442L5.9356 7.3497ZM12.9139 14.328L9.67246 11.0866C9.5613 11.3696 9.50026 11.6777 9.50026 12.0002C9.50026 13.3809 10.6196 14.5002 12.0003 14.5002C12.3227 14.5002 12.6309 14.4391 12.9139 14.328ZM20.8068 16.5925L19.376 15.1617C20.0319 14.2268 20.5154 13.1586 20.7777 12.0002C19.8603 7.94818 16.2359 5.00016 12.0003 5.00016C11.1544 5.00016 10.3329 5.11773 9.55249 5.33818L7.97446 3.76015C9.22127 3.26959 10.5793 3.00016 12.0003 3.00016C17.3924 3.00016 21.8784 6.87992 22.8189 12.0002C22.5067 13.6998 21.8038 15.2628 20.8068 16.5925ZM11.7229 7.50857C11.8146 7.50299 11.9071 7.50016 12.0003 7.50016C14.4855 7.50016 16.5003 9.51488 16.5003 12.0002C16.5003 12.0933 16.4974 12.1858 16.4919 12.2775L11.7229 7.50857Z', // eye-off-line
  close: 'M11.9997 10.5865L16.9495 5.63672L18.3637 7.05093L13.4139 12.0007L18.3637 16.9504L16.9495 18.3646L11.9997 13.4149L7.04996 18.3646L5.63574 16.9504L10.5855 12.0007L5.63574 7.05093L7.04996 5.63672L11.9997 10.5865Z', // close-line
};

/**
 * An icon as inline SVG that takes the text color. The paths are the line icons of Remix Icon
 * (https://remixicon.com, Apache License 2.0, Copyright (c) Remix Design), the same set the WebUI's own menu uses.
 */
export function iconHtml(name: IconName): string {
  return (
    `<svg data-icon="${name}" viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true" focusable="false">` +
    `<path d="${ICONS[name]}"/></svg>`
  );
}

/**
 * Translate literal text into the key events that type it on the peer.
 *
 * `press: true` is RustDesk's "down and up in one message" — no held state to
 * leave stuck if the session drops mid-string. ASCII goes as `chr`, anything
 * beyond as `unicode`, and the two control characters that matter in pasted
 * text (newline, tab) as their control keys. ControlKey values: Return=27,
 * Tab=31 — fixed by the protobuf, not by us. Carriage returns are dropped so
 * CRLF text does not double-newline.
 */
export function buildTypeCommands(text: string): UiCommand[] {
  const out: UiCommand[] = [];
  const press = (keyKind: 'chr' | 'control' | 'unicode', value: number): void => {
    out.push({ c: 'key', down: false, press: true, keyKind, value, modifiers: [] });
  };
  for (const ch of text) {
    if (ch === '\r') continue;
    if (ch === '\n') { press('control', 27); continue; }
    if (ch === '\t') { press('control', 31); continue; }
    const cp = ch.codePointAt(0);
    if (cp === undefined) continue;
    press(cp <= 127 ? 'chr' : 'unicode', cp);
  }
  return out;
}

export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
