import type { SessionState } from '../core/contracts';

export type VoiceCallUiState = 'idle' | 'preparing' | 'waiting' | 'connected';

export type VoiceCallUiModel = {
  disabled: boolean;
  label: 'Voice call' | 'End call';
  ariaLabel: 'Start voice call' | 'End voice call';
  status: string;
};

/**
 * Voice is independent of keyboard/mouse control, so View only is deliberately
 * absent from this model. RustDesk permits calls in view-only remote sessions.
 */
export function voiceCallUiModel(
  sessionState: SessionState,
  audioAllowed: boolean,
  callState: VoiceCallUiState,
  browserSupported = true,
): VoiceCallUiModel {
  const active = callState === 'waiting' || callState === 'connected';
  if (!browserSupported && !active) {
    return {
      disabled: true,
      label: 'Voice call',
      ariaLabel: 'Start voice call',
      status: 'Voice calls need Chrome or Edge over HTTPS',
    };
  }
  if (callState === 'preparing') {
    return {
      disabled: true,
      label: 'Voice call',
      ariaLabel: 'Start voice call',
      status: 'Requesting microphone…',
    };
  }
  const unavailable = sessionState !== 'streaming' || !audioAllowed;
  const status = callState === 'waiting'
    ? 'Waiting for the remote user…'
    : callState === 'connected'
      ? 'Voice call connected'
      : !audioAllowed
        ? 'Voice calls are not permitted by this device'
        : sessionState !== 'streaming'
          ? 'Voice calls require a connected session'
          : 'Voice calls are off';

  return {
    disabled: unavailable && !active,
    label: active ? 'End call' : 'Voice call',
    ariaLabel: active ? 'End voice call' : 'Start voice call',
    status,
  };
}
