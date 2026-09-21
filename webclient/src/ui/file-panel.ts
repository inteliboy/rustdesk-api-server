// RustDesk API Server web client — in-session file-transfer panel.
//
// Slides over the live desktop viewport (no separate window, no second login):
// the panel opens its own FILE_TRANSFER relay connection to the same peer,
// authenticating silently with the h1 credential the desktop session already
// proved. Dual-pane manager: local computer left,
// remote computer right, Send/Receive between them, transfer queue + event
// log at the bottom, close (×) returns to the desktop.
//
// Local side is the browser, so "local" means:
//   - Chromium: a real directory opened with showDirectoryPicker() — browse,
//     upload from and download into it (writes are atomic: data commits on
//     close, so a cancelled download never clobbers an existing file).
//   - Fallback: stage files with a picker/drag-drop for upload; downloads go
//     to the browser's Downloads folder.
//
// Wire protocol (second relay connection, ConnType.FILE_TRANSFER):
//   browse    read_dir -> dir(id=0)
//   download  send{id} -> dir(id) file list -> per file: digest -> our
//             send_confirm(offset) -> blocks (empty block = EOF) -> done(id)
//   upload    receive{id, files} -> per file: our digest -> peer confirm
//             (send_confirm, or digest{is_upload} asking us to decide) ->
//             blocks -> done(id, fileCount) -> peer echoes done
//   ops       create/remove_file/remove_dir/rename -> done/error ack
import './file-panel.css';
import type { FtDirectory, FtEntry, SessionConfig, SessionEvent, SessionState, UiCommand } from '../core/contracts';
import {
  FT_BLOCK_SIZE,
  formatBytes,
  isDirKind,
  joinRemote,
  parentRemote,
} from '../core/file-transfer';
import { escapeHtml, iconHtml, type IconName } from './common';

type PickerWindow = Window & {
  showDirectoryPicker?(opts?: { mode?: 'read' | 'readwrite' }): Promise<FileSystemDirectoryHandle>;
};

// queryPermission/requestPermission are WICG additions not yet in lib.dom.
type PermissionedHandle = FileSystemDirectoryHandle & {
  queryPermission?(desc: { mode: 'read' | 'readwrite' }): Promise<PermissionState>;
  requestPermission?(desc: { mode: 'read' | 'readwrite' }): Promise<PermissionState>;
};

const MEM_DOWNLOAD_WARN = 512 * 1024 * 1024; // fallback downloads buffer in RAM
const UPLOAD_BACKLOG_LIMIT = 8 * 1024 * 1024; // pause while ws send buffer exceeds this

type ConflictDecision = { action: 'overwrite' | 'skip' | 'cancel'; applyAll: boolean };
type ConflictPolicy = 'ask' | 'overwrite' | 'skip';

type LocalEntry = {
  kind: 'dir' | 'file';
  name: string;
  size: number;
  modifiedMs: number;
  handle?: FileSystemDirectoryHandle | FileSystemFileHandle;
  file?: File; // fallback staged file
};

type UploadFile = { rel: string; file: File };

type Side = 'local' | 'remote';
type SortKey = 'name' | 'size' | 'mod';
// One row of either pane, in the shape the list draws.
type ViewEntry = { name: string; dir: boolean; icon: IconName; size: number; mod: number; hidden: boolean };
const DRAG_TYPE = 'application/x-rd-ft';

type Job = {
  id: number;
  kind: 'download' | 'upload';
  label: string;
  status: 'starting' | 'running' | 'done' | 'error' | 'cancelled';
  error?: string;
  totalSize: number;
  doneBytes: number;
  fileCount: number;
  currentFile: string;
  policy: ConflictPolicy | null; // sticky per-job override from "apply to all"
  startMs: number;
  lastBytes: number; // for speed sampling
  lastSampleMs: number;
  speedBps: number;
  // download state
  remoteFiles?: FtEntry[];
  destDir?: FileSystemDirectoryHandle | null; // null = memory/anchor fallback
  curFileNum?: number;
  writable?: FileSystemWritableFileStream;
  memBuf?: Uint8Array[];
  writeChain?: Promise<void>;
  // upload state
  files?: UploadFile[];
  confirmWaiter?: (r: { skip: boolean; offsetBytes: number } | 'cancelled') => void;
  sentWaiter?: (buffered: number) => void;
  peerDone?: boolean;
};

type PendingConflict = {
  jobId: number;
  name: string;
  detail: string;
  resolve: (d: ConflictDecision) => void;
};

export type FilePanelOpts = {
  viewport: HTMLElement;
  workerUrl: string;
  toast: (msg: string) => void;
  // Built fresh on every (re)connect — must carry connType:'fileTransfer' and
  // the session's h1 credential; null when the desktop session isn't ready.
  getConfig: () => SessionConfig | null;
  // The window opened or closed (the dock button shows it, the remote screen takes the keys back).
  onVisibility?: (open: boolean) => void;
};

type Els = {
  panel: HTMLElement;
  status: HTMLElement;
  btnHidden: HTMLButtonElement;
  btnDrives: HTMLButtonElement;
  localSub: HTMLElement;
  localPath: HTMLInputElement;
  localList: HTMLElement;
  localBody: HTMLElement;
  localFoot: HTMLElement;
  localEmpty: HTMLElement;
  remoteSub: HTMLElement;
  remotePath: HTMLInputElement;
  remoteList: HTMLElement;
  remoteBody: HTMLElement;
  remoteFoot: HTMLElement;
  remoteEmpty: HTMLElement;
  btnSend: HTMLButtonElement;
  btnRecv: HTMLButtonElement;
  queue: HTMLElement;
  tabJobs: HTMLButtonElement;
  tabLog: HTMLButtonElement;
  jobsWrap: HTMLElement;
  logWrap: HTMLElement;
  dialog: HTMLElement;
};

function q<T extends Element>(scope: ParentNode, sel: string): T {
  const el = scope.querySelector<T>(sel);
  if (!el) throw new Error(`webclient: missing element ${sel}`);
  return el;
}

function nowMs(): number {
  return Date.now();
}

export class FilePanel {
  private readonly opts: FilePanelOpts;
  private el!: Els;
  private worker: Worker | undefined;
  private state: SessionState = 'closed';
  private peerLabel = '';
  private sep: '\\' | '/' = '/';
  private showHidden = false;
  private nextId = 1;
  private jobs: Job[] = [];
  private conflicts: PendingConflict[] = [];
  private conflictOpen = false;
  private ops = new Map<number, { desc: string; refresh: boolean }>();
  private ticker: ReturnType<typeof setInterval> | undefined;
  private beforeUnload = (): void => this.post({ c: 'disconnect' });

  // both panes
  private active: Side = 'remote';
  private cur: Record<Side, number> = { local: 0, remote: 0 }; // the cursor row; -1 is the ".." row
  private sort: Record<Side, { key: SortKey; dir: 1 | -1 }> = {
    local: { key: 'name', dir: 1 },
    remote: { key: 'name', dir: 1 },
  };
  private focusName: Record<Side, string | null> = { local: null, remote: null }; // where the cursor lands after a reload
  private localShown: FileSystemDirectoryHandle | undefined; // the folder the local list shows
  private typeBuf = '';
  private typeAt = 0;

  // remote pane
  private remotePathValue = '';
  private remoteEntries: FtEntry[] = [];
  private remoteSel = new Set<number>();
  private remoteAnchor = -1;

  // local pane
  private fsMode = false;
  private rootHandle: FileSystemDirectoryHandle | undefined;
  private dirStack: FileSystemDirectoryHandle[] = []; // [root, ..., cwd]
  private localEntries: LocalEntry[] = [];
  private staged: File[] = []; // fallback mode
  private localSel = new Set<number>();
  private localAnchor = -1;
  private localListGen = 0;

  constructor(opts: FilePanelOpts) {
    this.opts = opts;
    this.fsMode = typeof (window as PickerWindow).showDirectoryPicker === 'function';
    this.render();
    this.ticker = setInterval(() => this.tickJobs(), 500);
    window.addEventListener('beforeunload', this.beforeUnload);
    this.log('File transfer panel ready.');
    if (!this.fsMode) {
      this.log('This browser lacks the File System Access API — uploads use staged files, downloads go to the Downloads folder.');
    }
  }

  get isOpen(): boolean {
    return !this.el.panel.classList.contains('rd-ft-closed');
  }

  toggle(): void {
    if (this.isOpen) this.close();
    else this.open();
  }

  open(): void {
    this.el.panel.classList.remove('rd-ft-closed');
    this.opts.onVisibility?.(true);
    this.connectIfNeeded();
    this.focusActive();
  }

  close(): void {
    // Hide only — the file connection stays warm (the session core echoes the
    // peer's TestDelay keepalive) so reopening is instant and running
    // transfers continue in the background.
    this.el.panel.classList.add('rd-ft-closed');
    this.opts.onVisibility?.(false);
  }

  destroy(): void {
    this.post({ c: 'disconnect' });
    this.teardown();
    if (this.ticker) clearInterval(this.ticker);
    window.removeEventListener('beforeunload', this.beforeUnload);
    this.el.panel.remove();
  }

  // --- connection ------------------------------------------------------------

  private connectIfNeeded(): void {
    if (this.worker && this.state !== 'error' && this.state !== 'closed') return;
    const config = this.opts.getConfig();
    if (!config) {
      this.setStatus('error', 'Desktop session is not connected');
      return;
    }
    this.peerLabel = config.peerId;
    this.teardown();
    const worker = new Worker(this.opts.workerUrl, { type: 'module' });
    this.worker = worker;
    worker.onmessage = (e: MessageEvent) => this.onEvent(e.data as SessionEvent);
    worker.onerror = (e: ErrorEvent) => {
      this.state = 'error';
      this.setStatus('error', e.message || 'session worker failed');
    };
    this.post({ c: 'connectFile', config });
    this.state = 'connecting';
    this.setStatus('busy', 'Opening file channel…');
  }

  private teardown(): void {
    const w = this.worker;
    this.worker = undefined;
    if (w) setTimeout(() => w.terminate(), 250);
    for (const job of this.jobs) {
      if (job.status === 'running' || job.status === 'starting') {
        job.status = 'error';
        job.error = 'connection closed';
        job.confirmWaiter?.('cancelled');
        job.sentWaiter?.(0);
        void this.closeJobFile(job, false);
      }
    }
    this.renderJobs();
  }

  private post(cmd: UiCommand, transfer?: Transferable[]): void {
    if (!this.worker) return;
    if (transfer) this.worker.postMessage(cmd, transfer);
    else this.worker.postMessage(cmd);
  }

  // --- DOM -------------------------------------------------------------------

  private render(): void {
    const sBtn = (id: string, icon: IconName, title: string, label = ''): string =>
      `<button type="button" class="rd-btn${label ? ' rd-btn-text' : ''}" id="${id}" title="${title}" aria-label="${title}">` +
      `${iconHtml(icon)}${label ? `<span>${label}</span>` : ''}</button>`;

    // New folder, rename, delete: they act on the pane they sit in.
    const ops = (side: Side): string =>
      sBtn(`rd-ft-${side}-mkdir`, 'newFolder', 'New folder') +
      sBtn(`rd-ft-${side}-rename`, 'rename', 'Rename') +
      sBtn(`rd-ft-${side}-delete`, 'trash', 'Delete');

    const pane = (side: Side, title: string, icon: IconName, tools: string): string => `
      <section class="rd-ft-pane" id="rd-ft-${side}" data-side="${side}">
        <header class="rd-ft-pane-head">
          <span class="rd-ft-pane-ic">${iconHtml(icon)}</span>
          <strong>${title}</strong>
          <small id="rd-ft-${side}-sub"></small>
          <span class="rd-ft-pane-tools">${tools}</span>
        </header>
        <div class="rd-ft-pathrow">
          ${sBtn(`rd-ft-${side}-up`, 'arrowUp', 'Up one level (Backspace)')}
          <input id="rd-ft-${side}-path" class="rd-ft-path" type="text" spellcheck="false" autocomplete="off"
                 aria-label="${title} path" placeholder="${side === 'remote' ? 'Remote path' : ''}" ${side === 'local' ? 'readonly' : ''}>
          ${sBtn(`rd-ft-${side}-refresh`, 'refresh', 'Refresh')}
        </div>
        <div class="rd-ft-listwrap" id="rd-ft-${side}-list" tabindex="0" role="grid" aria-label="${title} files">
          <table class="rd-ft-table">
            <thead><tr>
              <th data-sort="name" title="Sort by name">Name<i class="rd-sort" aria-hidden="true"></i></th>
              <th class="rd-ft-col-size" data-sort="size" title="Sort by size">Size<i class="rd-sort" aria-hidden="true"></i></th>
              <th class="rd-ft-col-mod" data-sort="mod" title="Sort by date">Modified<i class="rd-sort" aria-hidden="true"></i></th>
            </tr></thead>
            <tbody id="rd-ft-${side}-body"></tbody>
          </table>
          <div class="rd-ft-empty" id="rd-ft-${side}-empty" hidden></div>
        </div>
        <footer class="rd-ft-foot" id="rd-ft-${side}-foot"></footer>
      </section>`;

    const panel = document.createElement('div');
    panel.id = 'rd-ft-overlay';
    panel.className = 'rd-ft-closed';
    panel.innerHTML = `
      <header class="rd-ft-head">
        <span class="rd-ft-title">${iconHtml('folderTransfer')}<span>File transfer</span></span>
        <span class="rd-ft-status" id="rd-ft-status"></span>
        <span class="rd-ft-head-spacer"></span>
        <button type="button" class="rd-chip" id="rd-ft-hidden" aria-pressed="false" title="Show hidden files">${iconHtml('eyeOff')}<span>Hidden files</span></button>
        ${sBtn('rd-ft-close', 'close', 'Close (Esc)')}
      </header>
      <div class="rd-ft-panes">
        ${pane(
          'local',
          'This computer',
          'monitor',
          sBtn('rd-ft-local-send', 'fileUpload', 'Send files… (pick files from anywhere)') +
            (this.fsMode
              ? sBtn('rd-ft-local-open', 'folderOpen', 'Open a local folder')
              : sBtn('rd-ft-local-add', 'folderOpen', 'Add files to stage')) +
            ops('local'),
        )}
        <div class="rd-ft-mid">
          <button type="button" class="rd-ft-go" id="rd-ft-send" title="Send the selection to the remote computer">
            ${iconHtml('sendRight')}<span>Send</span>
          </button>
          <button type="button" class="rd-ft-go" id="rd-ft-recv" title="Receive the selection from the remote computer">
            ${iconHtml('sendLeft')}<span>Receive</span>
          </button>
        </div>
        ${pane(
          'remote',
          'Remote computer',
          'folderTransfer',
          sBtn('rd-ft-remote-home', 'home', 'Home folder') + sBtn('rd-ft-remote-drives', 'drive', 'Drives') + ops('remote'),
        )}
      </div>
      <div class="rd-ft-queue" id="rd-ft-queue">
        <div class="rd-ft-tabs">
          <button type="button" class="rd-ft-tab rd-active" id="rd-ft-tab-jobs">Transfers</button>
          <button type="button" class="rd-ft-tab" id="rd-ft-tab-log">Event log</button>
          <span class="rd-ft-head-spacer"></span>
          ${sBtn('rd-ft-q-toggle', 'chevronDown', 'Hide the queue')}
        </div>
        <div class="rd-ft-jobs" id="rd-ft-jobs"><div class="rd-ft-none">No transfers yet.</div></div>
        <div class="rd-ft-log" id="rd-ft-log" hidden></div>
      </div>
      <div id="rd-ft-dialog" hidden></div>`;
    this.opts.viewport.appendChild(panel);

    const m = panel;
    this.el = {
      panel,
      status: q(m, '#rd-ft-status'),
      btnHidden: q(m, '#rd-ft-hidden'),
      localSub: q(m, '#rd-ft-local-sub'),
      localPath: q(m, '#rd-ft-local-path'),
      localList: q(m, '#rd-ft-local-list'),
      localBody: q(m, '#rd-ft-local-body'),
      localFoot: q(m, '#rd-ft-local-foot'),
      localEmpty: q(m, '#rd-ft-local-empty'),
      remoteSub: q(m, '#rd-ft-remote-sub'),
      remotePath: q(m, '#rd-ft-remote-path'),
      remoteList: q(m, '#rd-ft-remote-list'),
      remoteBody: q(m, '#rd-ft-remote-body'),
      remoteFoot: q(m, '#rd-ft-remote-foot'),
      remoteEmpty: q(m, '#rd-ft-remote-empty'),
      btnDrives: q(m, '#rd-ft-remote-drives'),
      btnSend: q(m, '#rd-ft-send'),
      btnRecv: q(m, '#rd-ft-recv'),
      queue: q(m, '#rd-ft-queue'),
      tabJobs: q(m, '#rd-ft-tab-jobs'),
      tabLog: q(m, '#rd-ft-tab-log'),
      jobsWrap: q(m, '#rd-ft-jobs'),
      logWrap: q(m, '#rd-ft-log'),
      dialog: q(m, '#rd-ft-dialog'),
    };

    this.el.localSub.textContent = this.fsMode ? 'no folder opened' : 'staged files';
    this.el.btnDrives.hidden = true;

    q<HTMLButtonElement>(m, '#rd-ft-close').addEventListener('click', () => this.close());
    this.el.btnHidden.addEventListener('click', () => {
      this.showHidden = !this.showHidden;
      this.el.btnHidden.innerHTML = iconHtml(this.showHidden ? 'eye' : 'eyeOff') + '<span>Hidden files</span>';
      this.el.btnHidden.title = this.showHidden ? 'Hide hidden files' : 'Show hidden files';
      this.el.btnHidden.classList.toggle('rd-on', this.showHidden);
      this.el.btnHidden.setAttribute('aria-pressed', String(this.showHidden));
      this.readRemote(this.remotePathValue);
      void this.refreshLocal();
    });

    q<HTMLButtonElement>(m, '#rd-ft-local-send').addEventListener('click', () => this.pickAndSendFiles());
    if (this.fsMode) {
      q<HTMLButtonElement>(m, '#rd-ft-local-open').addEventListener('click', () => void this.openLocalFolder());
    } else {
      q<HTMLButtonElement>(m, '#rd-ft-local-add').addEventListener('click', () => this.pickStagedFiles());
    }
    q<HTMLButtonElement>(m, '#rd-ft-local-up').addEventListener('click', () => void this.goUp('local'));
    q<HTMLButtonElement>(m, '#rd-ft-local-refresh').addEventListener('click', () => void this.refreshLocal());
    q<HTMLButtonElement>(m, '#rd-ft-remote-up').addEventListener('click', () => void this.goUp('remote'));
    q<HTMLButtonElement>(m, '#rd-ft-remote-home').addEventListener('click', () => this.readRemote(''));
    this.el.btnDrives.addEventListener('click', () => this.readRemote('/'));
    q<HTMLButtonElement>(m, '#rd-ft-remote-refresh').addEventListener('click', () => this.readRemote(this.remotePathValue));
    this.el.remotePath.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        this.readRemote(this.el.remotePath.value.trim());
        this.el.remoteList.focus();
      } else if (e.key === 'Escape') {
        this.el.remotePath.value = this.remotePathValue;
        this.el.remoteList.focus();
        e.stopPropagation();
      }
    });

    this.el.btnSend.addEventListener('click', () => this.transferFrom('local'));
    this.el.btnRecv.addEventListener('click', () => this.transferFrom('remote'));
    for (const side of ['local', 'remote'] as const) {
      q<HTMLButtonElement>(m, `#rd-ft-${side}-mkdir`).addEventListener('click', () => this.newFolder(side));
      q<HTMLButtonElement>(m, `#rd-ft-${side}-rename`).addEventListener('click', () => this.renameEntry(side));
      q<HTMLButtonElement>(m, `#rd-ft-${side}-delete`).addEventListener('click', () => this.deleteEntries(side));
    }

    this.wirePane('local');
    this.wirePane('remote');
    panel.addEventListener('keydown', (e) => this.onKey(e));

    const setTab = (tab: 'jobs' | 'log'): void => {
      this.el.tabJobs.classList.toggle('rd-active', tab === 'jobs');
      this.el.tabLog.classList.toggle('rd-active', tab === 'log');
      this.el.jobsWrap.hidden = tab !== 'jobs';
      this.el.logWrap.hidden = tab !== 'log';
      this.el.queue.classList.remove('rd-ft-q-collapsed');
    };
    this.el.tabJobs.addEventListener('click', () => setTab('jobs'));
    this.el.tabLog.addEventListener('click', () => setTab('log'));
    const qToggle = q<HTMLButtonElement>(m, '#rd-ft-q-toggle');
    qToggle.addEventListener('click', () => {
      const collapsed = this.el.queue.classList.toggle('rd-ft-q-collapsed');
      qToggle.title = collapsed ? 'Show the queue' : 'Hide the queue';
      qToggle.classList.toggle('rd-flip', collapsed);
    });

    this.updateLocalEmpty();
    this.renderPane('local');
    this.renderPane('remote');
    this.updateButtons();
  }

  private setStatus(kind: 'busy' | 'ok' | 'error', text: string): void {
    const s = this.el.status;
    s.className = `rd-ft-status rd-ft-status-${kind}`;
    s.innerHTML =
      (kind === 'busy' ? '<span class="rd-spinner" aria-hidden="true"></span>' : '') +
      `<span>${escapeHtml(text)}</span>` +
      (kind === 'error' ? '<button type="button" class="rd-ft-retry" id="rd-ft-retry">Retry</button>' : '');
    s.querySelector('#rd-ft-retry')?.addEventListener('click', () => this.connectIfNeeded());
  }

  // --- panes: rows, cursor, selection ----------------------------------------------

  private selOf(side: Side): Set<number> {
    return side === 'local' ? this.localSel : this.remoteSel;
  }

  private bodyOf(side: Side): HTMLElement {
    return side === 'local' ? this.el.localBody : this.el.remoteBody;
  }

  private listOf(side: Side): HTMLElement {
    return side === 'local' ? this.el.localList : this.el.remoteList;
  }

  private count(side: Side): number {
    return side === 'local' ? this.localEntries.length : this.remoteEntries.length;
  }

  /** The row above the first entry ("..") exists whenever the pane can go up a level. */
  private hasUp(side: Side): boolean {
    if (side === 'local') return this.fsMode && this.dirStack.length > 1;
    return this.remotePathValue !== '' && this.remotePathValue !== '/';
  }

  private minCursor(side: Side): number {
    return this.hasUp(side) ? -1 : 0;
  }

  private view(side: Side): ViewEntry[] {
    if (side === 'remote') {
      return this.remoteEntries.map((e) => ({
        name: e.name,
        dir: isDirKind(e.kind),
        icon: e.kind === 'drive' ? 'drive' : isDirKind(e.kind) ? 'folder' : 'file',
        size: e.size,
        mod: e.modifiedSec ? e.modifiedSec * 1000 : 0,
        hidden: !!e.isHidden,
      }));
    }
    return this.localEntries.map((e) => ({
      name: e.name,
      dir: e.kind === 'dir',
      icon: e.kind === 'dir' ? 'folder' : 'file',
      size: e.size,
      mod: e.modifiedMs,
      hidden: e.name.startsWith('.'),
    }));
  }

  /** What an action applies to: the marked entries, else the one under the cursor. */
  private targets(side: Side): number[] {
    const sel = this.selOf(side);
    if (sel.size) return [...sel].sort((a, b) => a - b);
    const c = this.cur[side];
    return c >= 0 && c < this.count(side) ? [c] : [];
  }

  private clampCursor(side: Side): void {
    const n = this.count(side);
    const min = this.minCursor(side);
    this.cur[side] = n === 0 ? min : Math.max(min, Math.min(n - 1, this.cur[side]));
  }

  private renderPane(side: Side): void {
    if (side === 'local' && !this.fsMode) {
      this.localEntries = this.staged.map((f) => ({
        kind: 'file' as const,
        name: f.name,
        size: f.size,
        modifiedMs: f.lastModified,
        file: f,
      }));
    }
    this.clampCursor(side);
    const list = this.view(side);
    const rows: string[] = [];
    if (this.hasUp(side)) {
      rows.push(
        `<tr data-idx="-1" class="rd-up"><td><span class="rd-ft-ic">${iconHtml('arrowUp')}</span>..</td>` +
          '<td class="rd-ft-col-size"></td><td class="rd-ft-col-mod"></td></tr>',
      );
    }
    list.forEach((e, i) => {
      const when = e.mod ? new Date(e.mod).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' }) : '';
      rows.push(
        `<tr data-idx="${i}" draggable="true"${e.hidden ? ' class="rd-hiddenfile"' : ''}>` +
          `<td><span class="rd-ft-ic" data-kind="${e.icon}">${iconHtml(e.icon)}</span><span class="rd-ft-name">${escapeHtml(e.name)}</span></td>` +
          `<td class="rd-ft-col-size">${e.dir ? '' : formatBytes(e.size)}</td><td class="rd-ft-col-mod">${when}</td></tr>`,
      );
    });
    this.bodyOf(side).innerHTML = rows.join('');
    this.paintHead(side);
    this.paintSel(side);
    if (side === 'remote') {
      this.el.remoteEmpty.hidden = this.remoteEntries.length > 0;
      this.el.remoteEmpty.textContent = this.state === 'streaming' ? 'Empty folder' : 'Not connected';
    } else {
      this.updateLocalEmpty();
    }
  }

  /** Marks the cursor row and the selected rows, and the footer, without rebuilding the list. */
  private paintSel(side: Side): void {
    const sel = this.selOf(side);
    const cur = this.cur[side];
    const list = this.view(side);
    for (const tr of this.bodyOf(side).querySelectorAll<HTMLElement>('tr[data-idx]')) {
      const i = Number(tr.dataset.idx);
      tr.classList.toggle('rd-sel', sel.has(i));
      tr.classList.toggle('rd-cur', i === cur);
      tr.setAttribute('aria-selected', String(sel.has(i)));
    }
    let size = 0;
    for (const i of sel) size += list[i]?.dir ? 0 : (list[i]?.size ?? 0);
    const foot = side === 'local' ? this.el.localFoot : this.el.remoteFoot;
    foot.textContent =
      sel.size > 0
        ? `${sel.size} of ${list.length} selected${size > 0 ? ` · ${formatBytes(size)}` : ''}`
        : `${list.length} item${list.length === 1 ? '' : 's'}`;
    this.bodyOf(side)
      .querySelector('tr.rd-cur')
      ?.scrollIntoView({ block: 'nearest' });
    this.updateButtons();
  }

  private paintHead(side: Side): void {
    const { key, dir } = this.sort[side];
    for (const th of this.listOf(side).querySelectorAll<HTMLElement>('th[data-sort]')) {
      const on = th.dataset.sort === key;
      th.classList.toggle('rd-sorted', on);
      const mark = th.querySelector('.rd-sort');
      if (mark) mark.innerHTML = on ? iconHtml(dir === 1 ? 'sortUp' : 'sortDown') : '';
    }
  }

  private setActive(side: Side): void {
    this.active = side;
    this.el.panel.querySelector('#rd-ft-local')?.classList.toggle('rd-ft-active', side === 'local');
    this.el.panel.querySelector('#rd-ft-remote')?.classList.toggle('rd-ft-active', side === 'remote');
    this.updateButtons();
  }

  private focusActive(): void {
    this.setActive(this.active);
    this.listOf(this.active).focus({ preventScroll: true });
  }

  private moveCursor(side: Side, to: number, extend: boolean, keepSel = false): void {
    const min = this.minCursor(side);
    const n = this.count(side);
    if (n === 0 && min === 0) return;
    const old = this.cur[side];
    const next = Math.max(min, Math.min(n - 1, to));
    const sel = this.selOf(side);
    if (extend) {
      const anchor = this.anchorOf(side) < 0 ? Math.max(old, 0) : this.anchorOf(side);
      this.setAnchor(side, anchor);
      sel.clear();
      for (let i = Math.min(anchor, next); i <= Math.max(anchor, next); i++) if (i >= 0) sel.add(i);
    } else {
      // A click-selection is a single row: moving the cursor moves it. Marks made with Space stay.
      if (!keepSel && sel.size === 1 && sel.has(old)) sel.clear();
      this.setAnchor(side, next);
    }
    this.cur[side] = next;
    this.paintSel(side);
  }

  private anchorOf(side: Side): number {
    return side === 'local' ? this.localAnchor : this.remoteAnchor;
  }

  private setAnchor(side: Side, i: number): void {
    if (side === 'local') this.localAnchor = i;
    else this.remoteAnchor = i;
  }

  private toggleMark(side: Side, i: number): void {
    if (i < 0 || i >= this.count(side)) return;
    const sel = this.selOf(side);
    if (sel.has(i)) sel.delete(i);
    else sel.add(i);
    this.setAnchor(side, i);
  }

  private wirePane(side: Side): void {
    const body = this.bodyOf(side);
    const list = this.listOf(side);
    const paneEl = q<HTMLElement>(this.el.panel, `#rd-ft-${side}`);
    paneEl.addEventListener('pointerdown', () => this.setActive(side));
    list.addEventListener('focus', () => this.setActive(side));

    body.addEventListener('click', (e) => {
      const tr = (e.target as HTMLElement).closest<HTMLElement>('tr[data-idx]');
      if (!tr) return;
      const idx = Number(tr.dataset.idx);
      this.setActive(side);
      const sel = this.selOf(side);
      if (idx < 0) {
        this.cur[side] = -1;
        sel.clear();
        this.paintSel(side);
        return;
      }
      const anchor = this.anchorOf(side);
      if (e.shiftKey && anchor >= 0) {
        sel.clear();
        for (let i = Math.min(anchor, idx); i <= Math.max(anchor, idx); i++) sel.add(i);
      } else if (e.metaKey || e.ctrlKey) {
        this.toggleMark(side, idx);
      } else {
        sel.clear();
        sel.add(idx);
        this.setAnchor(side, idx);
      }
      this.cur[side] = idx;
      this.paintSel(side);
    });
    body.addEventListener('dblclick', (e) => {
      const tr = (e.target as HTMLElement).closest<HTMLElement>('tr[data-idx]');
      if (!tr) return;
      this.cur[side] = Number(tr.dataset.idx);
      this.activateCursor(side);
    });

    // Sorting: a click on a column head; the same head again reverses.
    list.querySelectorAll<HTMLElement>('th[data-sort]').forEach((th) =>
      th.addEventListener('click', () => {
        const key = th.dataset.sort as SortKey;
        const cur = this.sort[side];
        this.sort[side] = { key, dir: cur.key === key ? (cur.dir === 1 ? -1 : 1) : 1 };
        this.resort(side);
      }),
    );

    // Drag rows onto the other pane to copy them there.
    body.addEventListener('dragstart', (e) => {
      const tr = (e.target as HTMLElement).closest<HTMLElement>('tr[data-idx]');
      const idx = tr ? Number(tr.dataset.idx) : -1;
      if (!tr || idx < 0 || !e.dataTransfer) {
        e.preventDefault();
        return;
      }
      const sel = this.selOf(side);
      if (!sel.has(idx)) {
        sel.clear();
        sel.add(idx);
        this.cur[side] = idx;
        this.paintSel(side);
      }
      e.dataTransfer.setData(DRAG_TYPE, side);
      e.dataTransfer.effectAllowed = 'copy';
    });

    // Drop: rows from the other pane, or files from the operating system.
    paneEl.addEventListener('dragover', (e) => {
      e.preventDefault();
      paneEl.classList.add('rd-ft-drop');
    });
    paneEl.addEventListener('dragleave', (e) => {
      if (!paneEl.contains(e.relatedTarget as Node | null)) paneEl.classList.remove('rd-ft-drop');
    });
    paneEl.addEventListener('drop', (e) => {
      e.preventDefault();
      paneEl.classList.remove('rd-ft-drop');
      const from = e.dataTransfer?.getData(DRAG_TYPE);
      if (from === 'local' || from === 'remote') {
        if (from !== side) this.transferFrom(from);
        return;
      }
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (!files.length) return;
      if (side === 'remote') void this.uploadFiles(files.map((f) => ({ rel: f.name, file: f })));
      else this.stageFiles(files);
    });
  }

  // --- keyboard (Total Commander style) --------------------------------------------

  private onKey(e: KeyboardEvent): void {
    if (!this.isOpen || !this.el.dialog.hidden) return;
    if (e.target instanceof HTMLInputElement) return; // the path field has its own keys
    const side = this.active;
    const ctrl = e.ctrlKey || e.metaKey;
    const cur = this.cur[side];
    const page = Math.max(3, Math.floor(this.listOf(side).clientHeight / 28) - 1);
    let handled = true;
    switch (e.key) {
      case 'Tab':
        this.setActive(side === 'local' ? 'remote' : 'local');
        this.listOf(this.active).focus({ preventScroll: true });
        break;
      case 'ArrowDown':
        this.moveCursor(side, cur + 1, e.shiftKey);
        break;
      case 'ArrowUp':
        this.moveCursor(side, cur - 1, e.shiftKey);
        break;
      case 'PageDown':
        this.moveCursor(side, cur + page, e.shiftKey);
        break;
      case 'PageUp':
        this.moveCursor(side, cur - page, e.shiftKey);
        break;
      case 'Home':
        this.moveCursor(side, 0, e.shiftKey);
        break;
      case 'End':
        this.moveCursor(side, this.count(side) - 1, e.shiftKey);
        break;
      case 'Enter':
        this.activateCursor(side);
        break;
      case 'Backspace':
        void this.goUp(side);
        break;
      case 'Insert':
      case ' ':
        if (e.key === ' ' && this.typeBuf) {
          this.typeAhead(' ');
          break;
        }
        this.toggleMark(side, cur);
        this.moveCursor(side, cur + 1, false, true);
        break;
      case 'a':
      case 'A':
        if (!ctrl) {
          this.typeAhead(e.key);
          break;
        }
        this.selOf(side).clear();
        for (let i = 0; i < this.count(side); i++) this.selOf(side).add(i);
        this.paintSel(side);
        break;
      case '*': {
        const sel = this.selOf(side);
        for (let i = 0; i < this.count(side); i++) {
          if (sel.has(i)) sel.delete(i);
          else sel.add(i);
        }
        this.paintSel(side);
        break;
      }
      case 'F2':
        this.renameEntry(side);
        break;
      case 'F5':
        this.copyToOther();
        break;
      case 'F7':
        this.newFolder(side);
        break;
      case 'F8':
      case 'Delete':
        this.deleteEntries(side);
        break;
      case 'Escape':
        this.close();
        break;
      default:
        handled = false;
        if (e.key.length === 1 && !ctrl && !e.altKey) {
          this.typeAhead(e.key);
          handled = true;
        }
    }
    if (handled) e.preventDefault();
  }

  /** Typing a name jumps the cursor to the next entry that starts with it. */
  private typeAhead(ch: string): void {
    const side = this.active;
    const now = nowMs();
    this.typeBuf = now - this.typeAt > 900 ? ch.toLowerCase() : this.typeBuf + ch.toLowerCase();
    this.typeAt = now;
    setTimeout(() => {
      if (nowMs() - this.typeAt >= 900) this.typeBuf = '';
    }, 950);
    const names = this.view(side).map((e) => e.name.toLowerCase());
    if (!names.length) return;
    const from = Math.max(0, this.cur[side] + (this.typeBuf.length === 1 ? 1 : 0));
    const order = [...names.keys()].sort(
      (a, b) => ((a - from + names.length) % names.length) - ((b - from + names.length) % names.length),
    );
    const hit = order.find((i) => names[i]!.startsWith(this.typeBuf));
    if (hit !== undefined) this.moveCursor(side, hit, false);
  }

  private activateCursor(side: Side): void {
    const i = this.cur[side];
    if (i < 0) {
      void this.goUp(side);
      return;
    }
    const v = this.view(side)[i];
    if (!v?.dir) return; // a file: transfers are F5 or a drag, never a stray double-click
    if (side === 'remote') {
      const e = this.remoteEntries[i];
      if (e) this.readRemote(joinRemote(this.remotePathValue, e.name, this.sep));
    } else {
      const e = this.localEntries[i];
      if (e && this.fsMode) void this.localEnter(e);
    }
  }

  private async goUp(side: Side): Promise<void> {
    if (!this.hasUp(side)) return;
    if (side === 'local') {
      this.focusName.local = this.localCwd()?.name ?? null;
      await this.localUp();
    } else {
      const parts = this.remotePathValue.split(this.sep).filter(Boolean);
      this.focusName.remote = parts[parts.length - 1] ?? null;
      this.remoteUp();
    }
  }

  private copyToOther(): void {
    this.transferFrom(this.active);
  }

  private transferFrom(from: Side): void {
    if (this.state !== 'streaming') {
      this.opts.toast('Not connected');
      return;
    }
    if (from === 'local') void this.startUpload();
    else this.startDownloads();
  }

  // --- sorting -----------------------------------------------------------------------

  private compareFor(side: Side): (a: ViewEntry, b: ViewEntry) => number {
    const { key, dir } = this.sort[side];
    return (a, b) => {
      if (a.dir !== b.dir) return a.dir ? -1 : 1; // folders stay on top
      const byName = a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true });
      if (key === 'name') return byName * dir;
      const d = key === 'size' ? a.size - b.size : a.mod - b.mod;
      return d !== 0 ? d * dir : byName;
    };
  }

  /** Sort the pane's entries in place; the cursor stays on the same name (or on `focusName`). */
  private sortEntries(side: Side, keepCursor = false): void {
    const curName = keepCursor ? (this.view(side)[this.cur[side]]?.name ?? null) : null;
    const cmp = this.compareFor(side);
    if (side === 'remote') {
      const views = new Map(this.remoteEntries.map((e, i) => [e, this.view('remote')[i]!] as const));
      this.remoteEntries.sort((a, b) => cmp(views.get(a)!, views.get(b)!));
    } else {
      const views = new Map(this.localEntries.map((e, i) => [e, this.view('local')[i]!] as const));
      this.localEntries.sort((a, b) => cmp(views.get(a)!, views.get(b)!));
    }
    const wanted = this.focusName[side] ?? curName;
    this.focusName[side] = null;
    const at = wanted === null ? -1 : this.view(side).findIndex((e) => e.name === wanted);
    this.cur[side] = at >= 0 ? at : this.count(side) === 0 ? this.minCursor(side) : 0;
  }

  private resort(side: Side): void {
    const names = new Set([...this.selOf(side)].map((i) => this.view(side)[i]?.name));
    this.sortEntries(side, true);
    const sel = this.selOf(side);
    sel.clear();
    this.view(side).forEach((e, i) => {
      if (names.has(e.name)) sel.add(i);
    });
    this.renderPane(side);
  }

  // --- folder, rename and delete in either pane ----------------------------------------

  private newFolder(side: Side): void {
    if (side === 'remote') {
      this.newRemoteFolder();
      return;
    }
    const cwd = this.localCwd();
    if (!this.fsMode || !cwd) {
      this.opts.toast('Open a local folder first');
      return;
    }
    this.promptDialog('New folder', 'Folder name', '', (name) => {
      if (!name) return;
      void (async () => {
        if (!(await this.ensureWritable(cwd))) {
          this.opts.toast('The browser did not allow writing to this folder');
          return;
        }
        try {
          await cwd.getDirectoryHandle(name, { create: true });
          this.log(`Created folder "${name}" on this computer.`);
          this.focusName.local = name;
          await this.refreshLocal();
        } catch (e) {
          this.opts.toast(`Could not create the folder: ${(e as Error).message}`);
        }
      })();
    });
  }

  private renameEntry(side: Side): void {
    const idx = this.targets(side);
    if (idx.length !== 1) {
      this.opts.toast('Put the cursor on one item to rename it');
      return;
    }
    if (side === 'remote') {
      this.renameRemote(idx[0]!);
      return;
    }
    const entry = this.localEntries[idx[0]!];
    const cwd = this.localCwd();
    const handle = entry?.handle as (FileSystemHandle & { move?: (name: string) => Promise<void> }) | undefined;
    if (!entry || !handle || !cwd) {
      this.opts.toast('Renaming is only possible in an opened local folder');
      return;
    }
    if (typeof handle.move !== 'function') {
      this.opts.toast('This browser cannot rename local files');
      return;
    }
    this.promptDialog(`Rename "${entry.name}"`, 'New name', entry.name, (name) => {
      if (!name || name === entry.name) return;
      void (async () => {
        if (!(await this.ensureWritable(cwd))) {
          this.opts.toast('The browser did not allow writing to this folder');
          return;
        }
        try {
          await handle.move!(name);
          this.log(`Renamed "${entry.name}" to "${name}" on this computer.`);
          this.focusName.local = name;
          await this.refreshLocal();
        } catch (e) {
          this.opts.toast(`Could not rename: ${(e as Error).message}`);
        }
      })();
    });
  }

  private deleteEntries(side: Side): void {
    const idx = this.targets(side);
    if (!idx.length) return;
    if (side === 'remote') {
      if (this.state !== 'streaming') return;
      const entries = idx.map((i) => this.remoteEntries[i]).filter((e): e is FtEntry => !!e);
      const what = entries.length === 1 ? `"${entries[0]!.name}"` : `${entries.length} items`;
      this.confirmDialog(`Delete ${what} from the remote computer? This cannot be undone.`, () => {
        for (const e of entries) {
          const path = joinRemote(this.remotePathValue, e.name, this.sep);
          const id = this.op(`Delete "${e.name}"`, true);
          if (isDirKind(e.kind)) this.post({ c: 'ftRemoveDir', id, path });
          else this.post({ c: 'ftRemoveFile', id, path, fileNum: 0 });
        }
      });
      return;
    }
    const entries = idx.map((i) => this.localEntries[i]).filter((e): e is LocalEntry => !!e);
    if (!this.fsMode) {
      // Staged files are only a list: removing one does not touch the file.
      const gone = new Set(entries.map((e) => e.file));
      this.staged = this.staged.filter((f) => !gone.has(f));
      this.localSel.clear();
      this.renderPane('local');
      return;
    }
    const cwd = this.localCwd();
    if (!cwd) return;
    const what = entries.length === 1 ? `"${entries[0]!.name}"` : `${entries.length} items`;
    this.confirmDialog(`Delete ${what} from this computer? This cannot be undone.`, () => {
      void (async () => {
        if (!(await this.ensureWritable(cwd))) {
          this.opts.toast('The browser did not allow writing to this folder');
          return;
        }
        for (const e of entries) {
          try {
            await cwd.removeEntry(e.name, { recursive: true });
            this.log(`Deleted "${e.name}" on this computer.`);
          } catch (err) {
            this.opts.toast(`Could not delete "${e.name}": ${(err as Error).message}`);
          }
        }
        await this.refreshLocal();
      })();
    });
  }

  private updateButtons(): void {
    if (!this.el?.btnSend) return;
    const connected = this.state === 'streaming';
    this.el.btnSend.disabled = !connected || this.targets('local').length === 0;
    this.el.btnRecv.disabled = !connected || this.targets('remote').length === 0;
    const set = (id: string, disabled: boolean): void => {
      const b = this.el.panel.querySelector<HTMLButtonElement>(`#${id}`);
      if (b) b.disabled = disabled;
    };
    const nl = this.targets('local').length;
    const nr = this.targets('remote').length;
    set('rd-ft-local-mkdir', !this.fsMode || !this.rootHandle);
    set('rd-ft-local-rename', nl !== 1 || !this.fsMode);
    set('rd-ft-local-delete', nl === 0);
    set('rd-ft-remote-mkdir', !connected);
    set('rd-ft-remote-rename', nr !== 1 || !connected);
    set('rd-ft-remote-delete', nr === 0 || !connected);
    this.el.btnDrives.hidden = this.sep !== '\\';
  }

  // --- session events --------------------------------------------------------

  private onEvent(ev: SessionEvent): void {
    switch (ev.t) {
      case 'state':
        this.state = ev.state;
        switch (ev.state) {
          case 'streaming':
            this.setStatus('ok', `Connected · ${this.peerLabel}`);
            this.log(`File channel to ${this.peerLabel} established.`);
            if (!this.remotePathValue) this.readRemote('');
            break;
          case 'error':
            this.teardown();
            this.setStatus('error', ev.detail || 'Connection failed');
            this.log(`File channel error: ${ev.detail || 'unknown'}`);
            break;
          case 'closed':
            this.teardown();
            this.setStatus('error', 'File channel closed');
            this.log('File channel closed.');
            break;
          case 'needAccept':
            this.setStatus('busy', 'Waiting for the remote user to accept…');
            break;
          default:
            this.setStatus('busy', 'Opening file channel…');
        }
        this.updateButtons();
        this.renderPane('remote');
        break;
      case 'peerInfo':
        this.el.remoteSub.textContent = ev.username ? `${ev.username}@${ev.hostname}` : ev.hostname;
        break;
      case 'loginError':
        // Shouldn't happen (same credential as the live desktop session), but
        // surface it honestly if the peer rejects the file channel.
        this.teardown();
        this.state = 'error';
        this.setStatus('error', ev.message || 'File channel login failed');
        this.log(`File channel login failed: ${ev.message}`);
        this.updateButtons();
        break;
      case 'msgbox': {
        const text = ev.text || ev.title;
        if (text) this.opts.toast(ev.title && ev.text ? `${ev.title}: ${ev.text}` : text);
        break;
      }
      case 'ftDir':
        this.onFtDir(ev.dir);
        break;
      case 'ftDigest':
        void this.onFtDigest(ev);
        break;
      case 'ftSendConfirm': {
        const job = this.jobById(ev.id);
        job?.confirmWaiter?.({ skip: ev.skip, offsetBytes: ev.offsetBytes });
        break;
      }
      case 'ftSent': {
        const job = this.jobById(ev.id);
        job?.sentWaiter?.(ev.buffered);
        break;
      }
      case 'ftBlock':
        this.onFtBlock(ev);
        break;
      case 'ftDone':
        this.onFtDone(ev.id, ev.fileNum);
        break;
      case 'ftError':
        this.onFtError(ev.id, ev.fileNum, ev.error);
        break;
      default:
        break;
    }
  }

  // --- remote pane -----------------------------------------------------------

  private readRemote(path: string): void {
    if (this.state !== 'streaming') return;
    this.post({ c: 'ftReadDir', path, includeHidden: this.showHidden });
  }

  private remoteUp(): void {
    const parent = parentRemote(this.remotePathValue, this.sep);
    // '' from a Windows drive root means "list the drives" (path "/").
    this.readRemote(parent === '' && this.sep === '\\' ? '/' : parent);
  }

  private onFtDir(dir: FtDirectory): void {
    // Directory listings for the browse pane come back with id=0 (and the
    // unsolicited post-login home listing). id>0 = a transfer job's file list.
    if (dir.id !== 0) {
      const job = this.jobById(dir.id);
      if (job && job.kind === 'download') {
        job.remoteFiles = dir.entries.map((e) => ({
          ...e,
          name: this.sep === '\\' ? e.name.replaceAll('\\', '/') : e.name,
        }));
        job.fileCount = job.remoteFiles.length;
        job.totalSize = job.remoteFiles.reduce((s, e) => s + e.size, 0);
        job.status = job.fileCount === 0 ? job.status : 'running';
        this.renderJobs();
      }
      return;
    }
    if (dir.path) {
      this.sep = dir.path.includes('\\') || /^[a-zA-Z]:/.test(dir.path) ? '\\' : '/';
    }
    // Reloading the folder we are in keeps the cursor where it was.
    if (dir.path === this.remotePathValue && !this.focusName.remote) {
      this.focusName.remote = this.view('remote')[this.cur.remote]?.name ?? null;
    }
    this.remotePathValue = dir.path;
    this.el.remotePath.value = dir.path;
    this.remoteEntries = dir.entries.filter((e) => this.showHidden || !e.isHidden);
    this.remoteSel.clear();
    this.remoteAnchor = -1;
    this.sortEntries('remote');
    this.renderPane('remote');
  }

  // --- remote ops ------------------------------------------------------------

  private op(desc: string, refresh: boolean): number {
    const id = this.nextId++;
    this.ops.set(id, { desc, refresh });
    return id;
  }

  private newRemoteFolder(): void {
    if (this.state !== 'streaming') return;
    this.promptDialog('New folder', 'Folder name', '', (name) => {
      if (!name) return;
      const id = this.op(`Create folder "${name}"`, true);
      this.post({ c: 'ftCreateDir', id, path: joinRemote(this.remotePathValue, name, this.sep) });
    });
  }

  private renameRemote(idx: number): void {
    const entry = this.remoteEntries[idx];
    if (!entry || this.state !== 'streaming') return;
    this.promptDialog(`Rename "${entry.name}"`, 'New name', entry.name, (name) => {
      if (!name || name === entry.name) return;
      const id = this.op(`Rename "${entry.name}" to "${name}"`, true);
      this.post({ c: 'ftRename', id, path: joinRemote(this.remotePathValue, entry.name, this.sep), newName: name });
    });
  }

  // --- local pane (File System Access mode) ----------------------------------

  private async openLocalFolder(): Promise<void> {
    const picker = (window as PickerWindow).showDirectoryPicker;
    if (!picker) return;
    try {
      // 'read' mode: Chrome's blocklist refuses readwrite grants on Desktop/
      // Documents etc. ("contains system files"). Reading is allowed almost
      // everywhere; write permission is requested per-folder when receiving.
      const handle = await picker.call(window, { mode: 'read' });
      this.rootHandle = handle;
      this.dirStack = [handle];
      this.localSel.clear();
      this.localAnchor = -1;
      await this.refreshLocal();
    } catch {
      return; // user cancelled the picker
    }
  }

  // True when Chrome grants (or the user approves) write access to `dir`.
  // Blocklisted folders auto-deny — callers fall back to Downloads saving.
  private async ensureWritable(dir: FileSystemDirectoryHandle): Promise<boolean> {
    const h = dir as PermissionedHandle;
    try {
      if (!h.queryPermission || !h.requestPermission) return true; // old impl: try and see
      if ((await h.queryPermission({ mode: 'readwrite' })) === 'granted') return true;
      return (await h.requestPermission({ mode: 'readwrite' })) === 'granted';
    } catch {
      return false;
    }
  }

  private localCwd(): FileSystemDirectoryHandle | undefined {
    return this.dirStack[this.dirStack.length - 1];
  }

  private async localEnter(entry: LocalEntry): Promise<void> {
    if (entry.kind !== 'dir' || !entry.handle) return;
    this.dirStack.push(entry.handle as FileSystemDirectoryHandle);
    this.localSel.clear();
    this.localAnchor = -1;
    await this.refreshLocal();
  }

  private async localUp(): Promise<void> {
    if (!this.fsMode || this.dirStack.length <= 1) return;
    this.dirStack.pop();
    this.localSel.clear();
    this.localAnchor = -1;
    await this.refreshLocal();
  }

  private async refreshLocal(): Promise<void> {
    if (!this.fsMode) {
      this.renderPane('local');
      return;
    }
    const cwd = this.localCwd();
    if (!cwd) {
      this.renderPane('local');
      return;
    }
    const gen = ++this.localListGen;
    const dirs: LocalEntry[] = [];
    const files: LocalEntry[] = [];
    try {
      for await (const handle of cwd.values()) {
        if (!this.showHidden && handle.name.startsWith('.')) continue;
        if (handle.kind === 'directory') {
          dirs.push({ kind: 'dir', name: handle.name, size: 0, modifiedMs: 0, handle: handle as FileSystemDirectoryHandle });
        } else {
          files.push({ kind: 'file', name: handle.name, size: 0, modifiedMs: 0, handle: handle as FileSystemFileHandle });
        }
      }
      await Promise.all(
        files.map(async (f) => {
          try {
            const file = await (f.handle as FileSystemFileHandle).getFile();
            f.size = file.size;
            f.modifiedMs = file.lastModified;
            f.file = file;
          } catch {
            /* unreadable entry — keep name only */
          }
        }),
      );
    } catch (e) {
      this.opts.toast(`Could not read folder: ${(e as Error).message}`);
    }
    if (gen !== this.localListGen) return; // superseded by a newer navigation
    // Reloading the folder we are in keeps the cursor where it was.
    if (this.localShown === cwd && !this.focusName.local) {
      this.focusName.local = this.view('local')[this.cur.local]?.name ?? null;
    }
    this.localShown = cwd;
    this.localEntries = [...dirs, ...files];
    this.localSel.clear();
    this.localAnchor = -1;
    this.sortEntries('local');
    this.el.localPath.value = this.dirStack.map((d) => d.name).join('/');
    this.el.localSub.textContent = ''; // the path field says where we are
    this.renderPane('local');
    this.updateButtons();
  }

  // --- local pane (fallback staging mode) -------------------------------------

  private pickStagedFiles(): void {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.addEventListener('change', () => this.stageFiles(Array.from(input.files ?? [])));
    input.click();
  }

  // "Send files…": pick files from anywhere (Desktop included — the file
  // picker has none of the directory blocklist restrictions) and upload them
  // straight to the current remote folder.
  private pickAndSendFiles(): void {
    if (this.state !== 'streaming') {
      this.opts.toast('Not connected');
      return;
    }
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.addEventListener('change', () => {
      const files = Array.from(input.files ?? []);
      if (files.length) void this.uploadFiles(files.map((f) => ({ rel: f.name, file: f })));
    });
    input.click();
  }

  private stageFiles(files: File[]): void {
    if (this.fsMode) return; // FS mode: local pane is a real folder, drops go via Send
    if (!files.length) return;
    this.staged.push(...files);
    this.renderPane('local');
    this.updateButtons();
  }

  private updateLocalEmpty(): void {
    const empty = this.el.localEmpty;
    if (this.localEntries.length > 0) {
      empty.hidden = true;
      return;
    }
    empty.hidden = false;
    if (this.fsMode) {
      empty.innerHTML = this.rootHandle
        ? 'Empty folder'
        : `<div class="rd-ft-cta">${iconHtml('folderOpen')}<p>Open a local folder to browse it here — or use
           <b>Send files…</b> above, or drag files onto the remote pane, to send from anywhere.</p>
           <button type="button" class="rd-ft-cta-btn" id="rd-ft-cta-open">Open folder…</button></div>`;
      empty.querySelector('#rd-ft-cta-open')?.addEventListener('click', () => void this.openLocalFolder());
    } else {
      empty.innerHTML = `<div class="rd-ft-cta">${iconHtml('folderOpen')}<p>Drop files here or add them to stage for sending.<br>
        Received files are saved to your Downloads folder.</p>
        <button type="button" class="rd-ft-cta-btn" id="rd-ft-cta-add">Add files…</button></div>`;
      empty.querySelector('#rd-ft-cta-add')?.addEventListener('click', () => this.pickStagedFiles());
    }
  }

  // --- transfers: download ---------------------------------------------------

  private startDownloads(): void {
    const entries = this.targets('remote').map((i) => this.remoteEntries[i]).filter((e): e is FtEntry => !!e);
    void this.startDownloadOf(entries);
  }

  private async startDownloadOf(entries: FtEntry[]): Promise<void> {
    if (this.state !== 'streaming' || !entries.length) return;
    let destDir = this.fsMode ? this.localCwd() ?? null : null;
    if (destDir && !(await this.ensureWritable(destDir))) {
      destDir = null;
      this.log('Chrome refused write access to this folder — received files will be saved to your Downloads folder instead.');
      this.opts.toast('Saving to your Downloads folder (folder is read-only)');
    } else if (this.fsMode && !destDir) {
      this.log('No local folder open — received files will be saved to your Downloads folder.');
    }
    for (const entry of entries) {
      const id = this.nextId++;
      const job: Job = {
        id,
        kind: 'download',
        label: entry.name,
        status: 'starting',
        totalSize: isDirKind(entry.kind) ? 0 : entry.size,
        doneBytes: 0,
        fileCount: isDirKind(entry.kind) ? 0 : 1,
        currentFile: entry.name,
        policy: null,
        startMs: nowMs(),
        lastBytes: 0,
        lastSampleMs: nowMs(),
        speedBps: 0,
        destDir,
        curFileNum: -1,
        memBuf: [],
        writeChain: Promise.resolve(),
      };
      this.jobs.unshift(job);
      this.post({
        c: 'ftSend',
        id,
        path: joinRemote(this.remotePathValue, entry.name, this.sep),
        includeHidden: this.showHidden,
        fileNum: 0,
      });
      this.log(`Receiving "${entry.name}"…`);
      if (!destDir && entry.size > MEM_DOWNLOAD_WARN) {
        this.log(`Warning: "${entry.name}" is ${formatBytes(entry.size)} and will be buffered in memory before saving.`);
      }
    }
    this.renderJobs();
  }

  // The peer announces each file with a digest and waits for our confirm.
  private async onFtDigest(ev: Extract<SessionEvent, { t: 'ftDigest' }>): Promise<void> {
    const job = this.jobById(ev.id);
    if (!job) return;
    if (job.kind === 'upload') {
      // Upload: the target exists on the remote side — our call.
      const decision = await this.resolveConflict(job, this.uploadFileName(job, ev.fileNum), ev);
      if (decision === 'cancel') {
        this.cancelJob(job);
        job.confirmWaiter?.('cancelled');
        return;
      }
      this.post({ c: 'ftConfirm', id: job.id, fileNum: ev.fileNum, skip: decision === 'skip', offsetBlk: 0 });
      job.confirmWaiter?.(decision === 'skip' ? { skip: true, offsetBytes: 0 } : { skip: false, offsetBytes: 0 });
      return;
    }
    // Download: digest describes the remote source file about to be sent.
    job.status = 'running';
    const rel = job.remoteFiles?.[ev.fileNum]?.name ?? job.label;
    job.currentFile = rel;
    let decision: 'overwrite' | 'skip' | 'cancel' = 'overwrite';
    if (job.destDir && (await this.localTargetExists(job.destDir, rel))) {
      decision = await this.resolveConflict(job, rel, ev);
    }
    if (decision === 'cancel') {
      this.cancelJob(job);
      return;
    }
    this.post({ c: 'ftConfirm', id: job.id, fileNum: ev.fileNum, skip: decision === 'skip', offsetBlk: 0 });
    if (decision === 'skip') this.log(`Skipped "${rel}" (already exists).`);
    this.renderJobs();
  }

  private async localTargetExists(dir: FileSystemDirectoryHandle, rel: string): Promise<boolean> {
    try {
      const parts = rel.split('/').filter(Boolean);
      let d = dir;
      for (const part of parts.slice(0, -1)) d = await d.getDirectoryHandle(part);
      await d.getFileHandle(parts[parts.length - 1]!);
      return true;
    } catch {
      return false;
    }
  }

  private onFtBlock(ev: Extract<SessionEvent, { t: 'ftBlock' }>): void {
    const job = this.jobById(ev.id);
    if (!job || job.kind !== 'download' || job.status === 'cancelled' || job.status === 'error') return;
    job.writeChain = (job.writeChain ?? Promise.resolve()).then(async () => {
      try {
        if (job.curFileNum !== ev.fileNum) {
          await this.closeJobFile(job, true);
          job.curFileNum = ev.fileNum;
          const rel = job.remoteFiles?.[ev.fileNum]?.name ?? job.label;
          job.currentFile = rel;
          if (job.destDir) {
            job.writable = await this.createLocalWritable(job.destDir, rel);
          } else {
            job.memBuf = [];
          }
        }
        if (ev.data.length === 0) {
          await this.closeJobFile(job, true);
          job.curFileNum = -1;
          return;
        }
        if (job.writable) await job.writable.write(ev.data as Uint8Array<ArrayBuffer>);
        else job.memBuf?.push(ev.data);
        job.doneBytes += ev.data.length;
      } catch (e) {
        job.status = 'error';
        job.error = `write failed: ${(e as Error).message}`;
        this.post({ c: 'ftCancel', id: job.id });
        this.log(`Receive failed for "${job.label}": ${job.error}`);
        this.renderJobs();
      }
    });
  }

  private async createLocalWritable(
    dir: FileSystemDirectoryHandle,
    rel: string,
  ): Promise<FileSystemWritableFileStream> {
    const parts = rel.split('/').filter(Boolean);
    let d = dir;
    for (const part of parts.slice(0, -1)) d = await d.getDirectoryHandle(part, { create: true });
    const fh = await d.getFileHandle(parts[parts.length - 1]!, { create: true });
    return fh.createWritable(); // data commits atomically on close()
  }

  // Finalize the file currently streaming into `job` (if any).
  private async closeJobFile(job: Job, commit: boolean): Promise<void> {
    const w = job.writable;
    job.writable = undefined;
    if (w) {
      try {
        if (commit) await w.close();
        else await w.abort();
      } catch {
        /* already closed */
      }
    }
    if (!job.destDir && commit && job.memBuf && job.curFileNum !== undefined && job.curFileNum >= 0) {
      const rel = job.remoteFiles?.[job.curFileNum]?.name ?? job.label;
      this.saveBlobToDownloads(rel.split('/').join('_'), job.memBuf);
    }
    job.memBuf = [];
  }

  private saveBlobToDownloads(name: string, chunks: Uint8Array[]): void {
    const blob = new Blob(chunks as BlobPart[]);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }

  private onFtDone(id: number, fileNum: number): void {
    const opInfo = this.ops.get(id);
    if (opInfo) {
      this.ops.delete(id);
      this.log(`${opInfo.desc} — done.`);
      if (opInfo.refresh) this.readRemote(this.remotePathValue);
      return;
    }
    const job = this.jobById(id);
    if (!job) return;
    if (job.kind === 'download') {
      job.writeChain = (job.writeChain ?? Promise.resolve()).then(async () => {
        await this.closeJobFile(job, true);
        job.curFileNum = -1;
        if (job.status !== 'error' && job.status !== 'cancelled') {
          job.status = 'done';
          job.doneBytes = Math.max(job.doneBytes, job.totalSize);
          this.log(`Received "${job.label}" (${formatBytes(job.doneBytes)}).`);
          this.opts.toast(`Received ${job.label}`);
          if (this.fsMode) void this.refreshLocal();
        }
        this.renderJobs();
      });
    } else {
      // Peer's write side echoed done — upload fully flushed on the far end.
      job.peerDone = true;
      if (job.status === 'running' || job.status === 'starting') {
        job.status = 'done';
        this.log(`Sent "${job.label}" (${formatBytes(job.doneBytes)}).`);
        this.opts.toast(`Sent ${job.label}`);
        this.readRemote(this.remotePathValue);
      }
      this.renderJobs();
    }
    void fileNum;
  }

  private onFtError(id: number, fileNum: number, error: string): void {
    const opInfo = this.ops.get(id);
    if (opInfo) {
      this.ops.delete(id);
      this.log(`${opInfo.desc} — failed: ${error}`);
      this.opts.toast(`${opInfo.desc} failed: ${error}`);
      if (opInfo.refresh) this.readRemote(this.remotePathValue);
      return;
    }
    const job = this.jobById(id);
    if (!job) return;
    // "skipped" for a single-file job is the peer acknowledging our skip.
    if (/^skipped$/i.test(error)) {
      job.status = 'done';
      this.renderJobs();
      return;
    }
    job.status = 'error';
    job.error = error;
    job.confirmWaiter?.('cancelled');
    job.sentWaiter?.(0);
    void this.closeJobFile(job, false);
    this.log(`Transfer "${job.label}" failed${fileNum >= 0 ? ` (file ${fileNum + 1})` : ''}: ${error}`);
    this.renderJobs();
  }

  // --- transfers: upload -----------------------------------------------------

  private async startUpload(): Promise<void> {
    if (this.state !== 'streaming') return;
    const files: UploadFile[] = [];
    const selected = this.targets('local').map((i) => this.localEntries[i]).filter((e): e is LocalEntry => !!e);
    for (const entry of selected) {
      if (entry.kind === 'file') {
        const f = entry.file ?? (entry.handle ? await (entry.handle as FileSystemFileHandle).getFile() : undefined);
        if (f) files.push({ rel: entry.name, file: f });
      } else if (entry.handle) {
        await this.collectDir(entry.handle as FileSystemDirectoryHandle, entry.name, files);
      }
    }
    await this.uploadFiles(files);
  }

  private async collectDir(dir: FileSystemDirectoryHandle, prefix: string, out: UploadFile[]): Promise<void> {
    for await (const handle of dir.values()) {
      if (!this.showHidden && handle.name.startsWith('.')) continue;
      if (handle.kind === 'directory') {
        await this.collectDir(handle as FileSystemDirectoryHandle, `${prefix}/${handle.name}`, out);
      } else {
        try {
          out.push({ rel: `${prefix}/${handle.name}`, file: await (handle as FileSystemFileHandle).getFile() });
        } catch {
          this.log(`Skipping unreadable file "${prefix}/${handle.name}".`);
        }
      }
    }
  }

  private async uploadFiles(files: UploadFile[]): Promise<void> {
    if (this.state !== 'streaming' || !files.length) return;
    const id = this.nextId++;
    const totalSize = files.reduce((s, f) => s + f.file.size, 0);
    const label = files.length === 1 ? files[0]!.rel : `${files.length} files`;
    const job: Job = {
      id,
      kind: 'upload',
      label,
      status: 'running',
      totalSize,
      doneBytes: 0,
      fileCount: files.length,
      currentFile: files[0]!.rel,
      policy: null,
      startMs: nowMs(),
      lastBytes: 0,
      lastSampleMs: nowMs(),
      speedBps: 0,
      files,
    };
    this.jobs.unshift(job);
    this.renderJobs();
    this.log(`Sending ${label} to ${this.remotePathValue || 'home'}…`);

    this.post({
      c: 'ftReceive',
      id,
      path: this.remotePathValue,
      files: files.map((f) => ({
        name: f.rel,
        size: f.file.size,
        modifiedSec: Math.floor(f.file.lastModified / 1000),
      })),
      totalSize,
      fileNum: 0,
    });

    try {
      for (let i = 0; i < files.length; i++) {
        if (this.jobStopped(job)) return;
        const { rel, file } = files[i]!;
        job.currentFile = rel;
        this.renderJobs();
        this.post({
          c: 'ftDigest',
          id,
          fileNum: i,
          fileSize: file.size,
          lastModifiedSec: Math.floor(file.lastModified / 1000),
        });
        // Wait for the go-ahead: either the peer's direct send_confirm (target
        // absent) or our own decision after its digest (handled in onFtDigest,
        // which resolves this same waiter).
        const confirm = await new Promise<{ skip: boolean; offsetBytes: number } | 'cancelled'>((resolve) => {
          job.confirmWaiter = resolve;
        });
        job.confirmWaiter = undefined;
        if (confirm === 'cancelled' || this.jobStopped(job)) return;
        if (confirm.skip) {
          job.doneBytes += file.size;
          this.log(`Skipped "${rel}" (already exists on remote).`);
          continue;
        }
        let offset = confirm.offsetBytes;
        job.doneBytes += Math.min(offset, file.size);
        while (offset < file.size) {
          if (this.jobStopped(job)) return;
          const end = Math.min(offset + FT_BLOCK_SIZE, file.size);
          const buf = new Uint8Array(await file.slice(offset, end).arrayBuffer());
          const buffered = await new Promise<number>((resolve) => {
            job.sentWaiter = resolve;
            this.post({ c: 'ftBlock', id, fileNum: i, data: buf, blkId: 0 }, [buf.buffer]);
          });
          job.sentWaiter = undefined;
          job.doneBytes += end - offset;
          offset = end;
          // Self-clock: if the socket backlog is deep, let TCP drain.
          if (buffered > UPLOAD_BACKLOG_LIMIT) {
            await new Promise((r) => setTimeout(r, 120));
          }
        }
        // Empty block = end-of-file marker.
        this.post({ c: 'ftBlock', id, fileNum: i, data: new Uint8Array(0), blkId: 0 });
      }
      if (this.jobStopped(job)) return;
      this.post({ c: 'ftDone', id, fileNum: files.length });
      // Completion is reported when the peer echoes done (onFtDone).
    } catch (e) {
      if (!this.jobStopped(job)) {
        job.status = 'error';
        job.error = (e as Error).message;
        this.post({ c: 'ftError', id, fileNum: -1, error: job.error ?? 'read failed' });
        this.log(`Send failed for "${job.label}": ${job.error}`);
        this.renderJobs();
      }
    }
  }

  private uploadFileName(job: Job, fileNum: number): string {
    return job.files?.[fileNum]?.rel ?? job.label;
  }

  private jobStopped(job: Job): boolean {
    return job.status === 'cancelled' || job.status === 'error' || this.state !== 'streaming';
  }

  // --- conflicts -------------------------------------------------------------

  private async resolveConflict(
    job: Job,
    name: string,
    ev: Extract<SessionEvent, { t: 'ftDigest' }>,
  ): Promise<'overwrite' | 'skip' | 'cancel'> {
    if (job.policy === 'overwrite') return 'overwrite';
    if (job.policy === 'skip') return 'skip';
    const detail =
      `${ev.fileSize ? formatBytes(ev.fileSize) : '—'} · modified ` +
      (ev.lastModifiedSec ? new Date(ev.lastModifiedSec * 1000).toLocaleString() : '—') +
      (job.kind === 'upload' ? ' (on remote)' : ' (incoming)');
    const decision = await new Promise<ConflictDecision>((resolve) => {
      this.conflicts.push({ jobId: job.id, name, detail, resolve });
      this.pumpConflicts();
    });
    if (decision.applyAll && decision.action !== 'cancel') job.policy = decision.action;
    return decision.action;
  }

  private pumpConflicts(): void {
    if (this.conflictOpen) return;
    const next = this.conflicts.shift();
    if (!next) return;
    this.conflictOpen = true;
    const d = this.el.dialog;
    d.hidden = false;
    d.innerHTML = `
      <div class="rd-ft-dlg">
        <h3>File already exists</h3>
        <p class="rd-ft-dlg-file">${iconHtml('file')}<span>${escapeHtml(next.name)}</span></p>
        <p class="rd-ft-dlg-detail">${escapeHtml(next.detail)}</p>
        <label class="rd-save rd-ft-dlg-all"><input type="checkbox" id="rd-ft-dlg-applyall"><span>Apply to all files in this transfer</span></label>
        <div class="rd-ft-dlg-btns">
          <button type="button" class="rd-ft-dlg-btn rd-ft-dlg-primary" data-act="overwrite">Overwrite</button>
          <button type="button" class="rd-ft-dlg-btn" data-act="skip">Skip</button>
          <button type="button" class="rd-ft-dlg-btn rd-ft-dlg-danger" data-act="cancel">Cancel transfer</button>
        </div>
      </div>`;
    const finish = (action: ConflictDecision['action']): void => {
      const applyAll = (d.querySelector('#rd-ft-dlg-applyall') as HTMLInputElement | null)?.checked ?? false;
      d.hidden = true;
      d.innerHTML = '';
      this.conflictOpen = false;
      next.resolve({ action, applyAll });
      this.pumpConflicts();
    };
    d.querySelectorAll<HTMLButtonElement>('[data-act]').forEach((b) =>
      b.addEventListener('click', () => finish(b.dataset.act as ConflictDecision['action'])),
    );
    d.querySelector<HTMLButtonElement>('[data-act="skip"]')?.focus();
  }

  // --- small dialogs ---------------------------------------------------------

  private promptDialog(title: string, placeholder: string, initial: string, cb: (v: string | null) => void): void {
    const d = this.el.dialog;
    d.hidden = false;
    d.innerHTML = `
      <div class="rd-ft-dlg">
        <h3>${escapeHtml(title)}</h3>
        <input type="text" class="rd-ft-dlg-input" id="rd-ft-dlg-input" placeholder="${escapeHtml(placeholder)}"
               value="${escapeHtml(initial)}" spellcheck="false" autocomplete="off">
        <div class="rd-ft-dlg-btns">
          <button type="button" class="rd-ft-dlg-btn rd-ft-dlg-primary" data-act="ok">OK</button>
          <button type="button" class="rd-ft-dlg-btn" data-act="no">Cancel</button>
        </div>
      </div>`;
    const input = q<HTMLInputElement>(d, '#rd-ft-dlg-input');
    input.focus();
    input.select();
    const finish = (ok: boolean): void => {
      const v = ok ? input.value.trim() : null;
      d.hidden = true;
      d.innerHTML = '';
      cb(v);
    };
    d.querySelector('[data-act="ok"]')!.addEventListener('click', () => finish(true));
    d.querySelector('[data-act="no"]')!.addEventListener('click', () => finish(false));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') finish(true);
      if (e.key === 'Escape') finish(false);
    });
  }

  private confirmDialog(text: string, cb: () => void): void {
    const d = this.el.dialog;
    d.hidden = false;
    d.innerHTML = `
      <div class="rd-ft-dlg">
        <h3>Are you sure?</h3>
        <p class="rd-ft-dlg-detail">${escapeHtml(text)}</p>
        <div class="rd-ft-dlg-btns">
          <button type="button" class="rd-ft-dlg-btn rd-ft-dlg-danger" data-act="ok">Delete</button>
          <button type="button" class="rd-ft-dlg-btn" data-act="no">Cancel</button>
        </div>
      </div>`;
    const finish = (ok: boolean): void => {
      d.hidden = true;
      d.innerHTML = '';
      if (ok) cb();
    };
    d.querySelector('[data-act="ok"]')!.addEventListener('click', () => finish(true));
    d.querySelector('[data-act="no"]')!.addEventListener('click', () => finish(false));
    d.onkeydown = (e) => {
      if (e.key === 'Escape') finish(false);
    };
    d.querySelector<HTMLButtonElement>('[data-act="no"]')?.focus(); // Enter does not delete by accident
  }

  // --- job list / log --------------------------------------------------------

  private jobById(id: number): Job | undefined {
    return this.jobs.find((j) => j.id === id);
  }

  private cancelJob(job: Job): void {
    if (job.status === 'done' || job.status === 'cancelled') return;
    job.status = 'cancelled';
    this.post({ c: 'ftCancel', id: job.id });
    job.confirmWaiter?.('cancelled');
    job.sentWaiter?.(0);
    void this.closeJobFile(job, false);
    this.log(`Cancelled "${job.label}".`);
    this.renderJobs();
  }

  private tickJobs(): void {
    let active = false;
    const t = nowMs();
    for (const job of this.jobs) {
      if (job.status !== 'running' && job.status !== 'starting') continue;
      active = true;
      const dt = t - job.lastSampleMs;
      if (dt >= 900) {
        const inst = ((job.doneBytes - job.lastBytes) / dt) * 1000;
        job.speedBps = job.speedBps === 0 ? inst : job.speedBps * 0.5 + inst * 0.5;
        job.lastBytes = job.doneBytes;
        job.lastSampleMs = t;
      }
    }
    if (active) this.renderJobs();
  }

  private renderJobs(): void {
    const wrap = this.el.jobsWrap;
    if (!this.jobs.length) {
      wrap.innerHTML = '<div class="rd-ft-none">No transfers yet.</div>';
      return;
    }
    wrap.innerHTML = this.jobs
      .map((j) => {
        const pct = j.totalSize > 0 ? Math.min(100, Math.round((j.doneBytes / j.totalSize) * 100)) : j.status === 'done' ? 100 : 0;
        const dirIcon = j.kind === 'download' ? iconHtml('sendLeft') : iconHtml('sendRight');
        const status =
          j.status === 'running' || j.status === 'starting'
            ? `${formatBytes(j.doneBytes)} of ${j.totalSize ? formatBytes(j.totalSize) : '?'}` +
              (j.speedBps > 0 ? ` · ${formatBytes(j.speedBps)}/s` : '')
            : j.status === 'done'
              ? `Completed · ${formatBytes(Math.max(j.doneBytes, j.totalSize))}`
              : j.status === 'cancelled'
                ? 'Cancelled'
                : `Failed: ${j.error ?? 'unknown error'}`;
        const cancellable = j.status === 'running' || j.status === 'starting';
        return `<div class="rd-ft-job rd-ft-job-${j.status}">
          <span class="rd-ft-job-dir">${dirIcon}</span>
          <div class="rd-ft-job-main">
            <div class="rd-ft-job-top"><span class="rd-ft-job-name">${escapeHtml(j.label)}</span>
              <span class="rd-ft-job-status">${escapeHtml(status)}</span></div>
            <div class="rd-ft-bar"><div class="rd-ft-bar-fill" style="width:${pct}%"></div></div>
          </div>
          ${cancellable ? `<button type="button" class="rd-btn rd-ft-job-cancel" data-job="${j.id}" title="Cancel">${iconHtml('close')}</button>` : ''}
        </div>`;
      })
      .join('');
    wrap.querySelectorAll<HTMLButtonElement>('[data-job]').forEach((b) =>
      b.addEventListener('click', () => {
        const job = this.jobById(Number(b.dataset.job));
        if (job) this.cancelJob(job);
      }),
    );
  }

  private log(message: string): void {
    const line = document.createElement('div');
    line.className = 'rd-ft-logline';
    const ts = new Date().toLocaleTimeString();
    line.textContent = `${ts}  ${message}`;
    this.el.logWrap.appendChild(line);
    this.el.logWrap.scrollTop = this.el.logWrap.scrollHeight;
  }

}
