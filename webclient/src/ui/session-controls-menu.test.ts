import { describe, expect, it } from 'vitest';
import { buildSecurityControlMenu, privacyMethodName } from './session-controls-menu';
import { codecLabel } from './display-controls';

const base = {
  platform: 'Windows',
  permissions: {},
  privacyModeSupported: true,
  privacyModeImpls: [],
  privacyModeOn: false,
  blockInputOn: false,
  lockAfterSessionEnd: false,
};

describe('remote device menu', () => {
  it('has no label that shows a raw key with underscores', () => {
    const items = buildSecurityControlMenu({
      ...base,
      privacyModeImpls: [
        { key: 'privacy_mode_impl_mag', label: 'privacy_mode_impl_mag_tip' },
        { key: 'privacy_mode_impl_virtual_display', label: 'privacy_mode_impl_virtual_display_tip' },
        { key: 'privacy_mode_impl_something_new', label: 'privacy_mode_impl_something_new_tip' },
      ],
    });
    for (const item of items) expect(item.label).not.toMatch(/_/);
    expect(items.map((i) => i.label)).toContain('Privacy mode (magnifier method)');
    expect(items.map((i) => i.label)).toContain('Privacy mode (virtual display method)');
    expect(items.map((i) => i.label)).toContain('Privacy mode (something new)');
  });

  it('names the privacy method only when there is a choice', () => {
    const one = buildSecurityControlMenu({
      ...base,
      privacyModeImpls: [{ key: 'privacy_mode_impl_mag', label: 'privacy_mode_impl_mag_tip' }],
    });
    expect(one.map((i) => i.label)).toContain('Privacy mode');
    const none = buildSecurityControlMenu(base);
    expect(none.map((i) => i.label)).toContain('Privacy mode');
  });

  it('every item explains itself', () => {
    for (const item of buildSecurityControlMenu(base)) expect(item.hint).toBeTruthy();
  });

  it('keeps a human label a device sends, and makes an unknown key readable', () => {
    expect(privacyMethodName('x', 'Screen curtain')).toBe('screen curtain');
    expect(privacyMethodName('privacy_mode_impl_other_thing', '')).toBe('other thing');
    expect(privacyMethodName('', '')).toBe('default method');
  });

  it('offers only the lock on a device that is not a desktop', () => {
    const items = buildSecurityControlMenu({ ...base, platform: 'Android' });
    expect(items.map((i) => i.id)).toEqual(['lockScreen']);
  });
});

describe('codecLabel', () => {
  it('names codecs for a person', () => {
    expect(codecLabel('h264')).toBe('H.264');
    expect(codecLabel('h265')).toBe('H.265 (HEVC)');
    expect(codecLabel('h265', true)).toBe('H.265');
    expect(codecLabel('auto')).toBe('Automatic');
    expect(codecLabel('vp9')).toBe('VP9');
    expect(codecLabel('')).toBe('—');
    expect(codecLabel('xyz')).toBe('XYZ');
  });
});
