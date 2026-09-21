import type { UiCommand } from '../core/contracts';
import { ControlKey } from '../gen/message';

export type PrivacyModeMenuImpl = { key: string; label: string };
/** `hint` is the longer explanation shown as the item's tooltip. */
export type SecurityControlMenuItem = { id: string; label: string; checked: boolean; hint?: string };

export type SecurityControlMenuInput = {
  platform: string;
  permissions: Record<string, boolean>;
  privacyModeSupported: boolean;
  privacyModeImpls: PrivacyModeMenuImpl[];
  privacyModeOn: boolean;
  activePrivacyImplKey?: string;
  blockInputOn: boolean;
  lockAfterSessionEnd: boolean;
  viewOnly?: boolean;
};

// What the remote device's privacy modes are called. The device sends a key and a translation key of its own
// (`privacy_mode_impl_mag`, `privacy_mode_impl_mag_tip`), which say nothing to a person.
const PRIVACY_METHOD_NAMES: Record<string, string> = {
  privacy_mode_impl_mag: 'magnifier method',
  privacy_mode_impl_virtual_display: 'virtual display method',
  privacy_mode_impl_exclude_capture: 'exclude-from-capture method',
};

/** A readable name for one privacy mode method; an unknown key is made readable, not shown raw. */
export function privacyMethodName(key: string, label: string): string {
  const known = PRIVACY_METHOD_NAMES[key];
  if (known) return known;
  const text = label && !label.includes('_') ? label : key;
  const plain = text
    .replace(/^privacy_mode_impl_/, '')
    .replace(/_tip$/, '')
    .replace(/_/g, ' ')
    .trim();
  return plain ? plain.toLowerCase() : 'default method';
}

export function buildLockScreenKeyCommand(): Extract<UiCommand, { c: 'key' }> {
  return { c: 'key', down: false, press: true, keyKind: 'control', value: ControlKey.LockScreen, modifiers: [] };
}

export function buildSecurityControlMenu(input: SecurityControlMenuInput): SecurityControlMenuItem[] {
  const platform = input.platform.toLowerCase();
  const desktop = platform.includes('windows') || platform.includes('linux') || platform.includes('mac');
  const canLockScreen = !input.viewOnly && input.permissions.Keyboard !== false;
  const lockScreenItem = {
    id: 'lockScreen',
    label: 'Lock the remote screen',
    checked: false,
    hint: 'Locks the remote computer now; its user has to sign in again',
  };
  if (!desktop) return canLockScreen ? [lockScreenItem] : [];

  const items: SecurityControlMenuItem[] = [];
  if (input.permissions.Restart !== false) {
    items.push({
      id: 'restart',
      label: 'Restart the remote device',
      checked: false,
      hint: 'Restarts the remote computer, then this page reconnects by itself',
    });
  }
  if (platform.includes('windows') && input.permissions.Keyboard !== false) {
    items.push({
      id: 'elevation',
      label: 'Ask for administrator rights',
      checked: false,
      hint: 'Asks the remote user to approve running RustDesk as administrator',
    });
  }

  if ((input.permissions.PrivacyMode !== false || input.privacyModeOn) && input.privacyModeSupported) {
    const impls = input.privacyModeImpls.length > 0
      ? input.privacyModeImpls
      : [{ key: 'privacy_mode_impl_mag', label: '' }];
    for (const impl of impls) {
      const checked = input.privacyModeOn
        && (!input.activePrivacyImplKey || input.activePrivacyImplKey === impl.key);
      items.push({
        id: `privacy:${impl.key}`,
        // The method is named only when the device offers a choice of them.
        label: input.privacyModeImpls.length > 1
          ? `Privacy mode (${privacyMethodName(impl.key, impl.label)})`
          : 'Privacy mode',
        checked,
        hint: 'Blanks the remote computer’s own display while you work, so nobody there can watch',
      });
    }
  }

  if (platform.includes('windows') && (input.permissions.BlockInput !== false || input.blockInputOn)) {
    items.push({
      id: 'blockInput',
      label: 'Block the remote user’s keyboard and mouse',
      checked: input.blockInputOn,
      hint: 'The person at the remote computer cannot type or move the mouse while this is on',
    });
  }
  if (canLockScreen) {
    items.push(lockScreenItem);
  }
  if (input.permissions.Keyboard !== false) {
    items.push({
      id: 'lockAfterSessionEnd',
      label: 'Lock the screen when I disconnect',
      checked: input.lockAfterSessionEnd,
      hint: 'Asks the remote computer to lock when this session ends (best effort; it does not confirm)',
    });
  }
  return items;
}
