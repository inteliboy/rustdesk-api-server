export type RemoteInputChannel = 'pointer' | 'keyboard' | 'clipboard' | 'terminal';

/**
 * View-only is a fail-closed boundary for every channel that can act on the
 * remote device. Read-only media and display controls do not use this policy.
 */
export function remoteInputAllowed(viewOnly: boolean, _channel: RemoteInputChannel): boolean {
  return !viewOnly;
}
