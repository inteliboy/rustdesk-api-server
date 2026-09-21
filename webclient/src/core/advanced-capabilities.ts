export type AdvancedPeerCapabilities = {
  viewCamera: boolean;
};

export function parseAdvancedPeerCapabilities(platformAdditions: string): AdvancedPeerCapabilities {
  try {
    const parsed: unknown = JSON.parse(platformAdditions);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { viewCamera: false };
    const record = parsed as Record<string, unknown>;
    return { viewCamera: record.support_view_camera === true };
  } catch {
    return { viewCamera: false };
  }
}
