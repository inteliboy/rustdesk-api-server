import {
  ElevationRequest,
  OptionMessage,
  OptionMessage_BoolOption,
  TogglePrivacyMode,
  type Misc,
} from '../gen/message';

type MiscControl = NonNullable<Misc['union']>;

export const RESTART_RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 12_000, 15_000] as const;
export const RESTART_RECONNECT_TIMEOUT_MS = 120_000;

export type PrivacyModeImpl = { key: string; label: string };

export function parsePrivacyModeImpls(platformAdditions: string): PrivacyModeImpl[] {
  if (!platformAdditions) return [];
  try {
    const parsed = JSON.parse(platformAdditions) as { supported_privacy_mode_impl?: unknown };
    if (!Array.isArray(parsed.supported_privacy_mode_impl)) return [];
    return parsed.supported_privacy_mode_impl.flatMap((entry) => {
      if (!Array.isArray(entry) || typeof entry[0] !== 'string' || typeof entry[1] !== 'string') return [];
      return [{ key: entry[0], label: entry[1] }];
    });
  } catch {
    return [];
  }
}

export function nextRestartReconnectDelay(
  attempt: number,
  elapsedMs: number,
  timeoutMs = RESTART_RECONNECT_TIMEOUT_MS,
): number | null {
  if (elapsedMs >= timeoutMs) return null;
  const delay = RESTART_RECONNECT_DELAYS_MS[attempt] ?? RESTART_RECONNECT_DELAYS_MS.at(-1)!;
  return Math.min(delay, timeoutMs - elapsedMs);
}

export function buildRestartRemoteDevice(): MiscControl {
  return { $case: 'restart_remote_device', restart_remote_device: true };
}

export function buildDirectElevation(): MiscControl {
  return {
    $case: 'elevation_request',
    elevation_request: ElevationRequest.fromPartial({
      union: { $case: 'direct', direct: true },
    }),
  };
}

export function buildPrivacyToggle(implKey: string, on: boolean): MiscControl {
  return {
    $case: 'toggle_privacy_mode',
    toggle_privacy_mode: TogglePrivacyMode.fromPartial({ impl_key: implKey, on }),
  };
}

export function buildBlockInputOption(on: boolean): OptionMessage {
  return OptionMessage.fromPartial({
    block_input: on ? OptionMessage_BoolOption.Yes : OptionMessage_BoolOption.No,
  });
}

export function buildLockAfterSessionEndOption(on: boolean): OptionMessage {
  return OptionMessage.fromPartial({
    lock_after_session_end: on ? OptionMessage_BoolOption.Yes : OptionMessage_BoolOption.No,
  });
}
