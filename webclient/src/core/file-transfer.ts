// File-transfer core: pure conversions between the protobuf FileResponse tree
// and the plain-object SessionEvent surface, plus remote-path arithmetic shared
// by the UI. Sans-IO — the worker owns sockets/cipher, the UI owns jobs.
import type { FtDirectory, FtEntry, FtEntryKind, SessionEvent } from './contracts';
import { FileType, type FileDirectory, type FileEntry, type FileResponse } from '../gen/message';

// RustDesk streams files in fixed blocks; the value is not negotiated on the
// wire, it just bounds per-message size. Keep comfortably under relay frame
// limits and match the native client's granularity.
export const FT_BLOCK_SIZE = 128 * 1024;

const KIND: Record<number, FtEntryKind> = {
  [FileType.Dir]: 'dir',
  [FileType.DirLink]: 'dirLink',
  [FileType.DirDrive]: 'drive',
  [FileType.File]: 'file',
  [FileType.FileLink]: 'fileLink',
};

export function entryFromProto(e: FileEntry): FtEntry {
  return {
    kind: KIND[e.entry_type] ?? 'file',
    name: e.name,
    size: Number(e.size),
    modifiedSec: Number(e.modified_time),
    isHidden: e.is_hidden,
  };
}

export function dirFromProto(d: FileDirectory): FtDirectory {
  return { id: d.id, path: d.path, entries: d.entries.map(entryFromProto) };
}

export function isDirKind(kind: FtEntryKind): boolean {
  return kind === 'dir' || kind === 'dirLink' || kind === 'drive';
}

// Convert one inbound FileResponse into UI events. `decompress` inflates
// zstd-compressed blocks so the UI never sees compressed payloads.
export function fileResponseToEvents(
  fr: FileResponse,
  decompress: (data: Uint8Array) => Uint8Array,
): SessionEvent[] {
  const u = fr.union;
  switch (u?.$case) {
    case 'dir':
      return [{ t: 'ftDir', dir: dirFromProto(u.dir) }];
    case 'block': {
      const b = u.block;
      let data = b.data;
      if (b.compressed) {
        try {
          data = decompress(b.data);
        } catch {
          return [{ t: 'ftError', id: b.id, fileNum: b.file_num, error: 'failed to decompress block' }];
        }
      }
      return [{ t: 'ftBlock', id: b.id, fileNum: b.file_num, data, blkId: b.blk_id }];
    }
    case 'done':
      return [{ t: 'ftDone', id: u.done.id, fileNum: u.done.file_num }];
    case 'error':
      return [{ t: 'ftError', id: u.error.id, fileNum: u.error.file_num, error: u.error.error }];
    case 'digest': {
      const d = u.digest;
      return [
        {
          t: 'ftDigest',
          id: d.id,
          fileNum: d.file_num,
          lastModifiedSec: Number(d.last_modified),
          fileSize: Number(d.file_size),
          isUpload: d.is_upload,
          isIdentical: d.is_identical,
          transferredSize: Number(d.transferred_size),
          isResume: d.is_resume,
        },
      ];
    }
    default:
      return [];
  }
}

// --- remote path arithmetic --------------------------------------------------
// The peer reports its platform in PeerInfo; Windows paths use "\" and have
// drive roots ("C:\"), everything else is "/".

export function sepForPlatform(platform: string): '\\' | '/' {
  return /windows/i.test(platform) ? '\\' : '/';
}

export function joinRemote(dir: string, name: string, sep: '\\' | '/'): string {
  if (dir === '') return name;
  if (dir.endsWith(sep)) return dir + name;
  return dir + sep + name;
}

// Parent of a remote path; '' means "the roots view" (drive list on Windows).
export function parentRemote(path: string, sep: '\\' | '/'): string {
  const trimmed = path.endsWith(sep) && path.length > 1 ? path.slice(0, -1) : path;
  const idx = trimmed.lastIndexOf(sep);
  if (sep === '\\') {
    // "C:\" (or "C:") is a root — up goes to the drive list.
    if (/^[a-zA-Z]:\\?$/.test(trimmed)) return '';
    if (idx <= 2) return trimmed.slice(0, 3); // "C:\foo" -> "C:\"
    return trimmed.slice(0, idx);
  }
  if (idx < 0) return '/';
  if (idx === 0) return '/';
  return trimmed.slice(0, idx);
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n;
  let u = -1;
  do {
    v /= 1024;
    u++;
  } while (v >= 1024 && u < units.length - 1);
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[u]}`;
}
