import type { UiCommand } from '../core/contracts';

const ADVANCED_DISCONNECT_FLUSH_MS = 250;

/**
 * Ask an independent session to disconnect, then terminate it after a short,
 * bounded flush window. postMessage only queues the command; terminating in the
 * same task can prevent the worker from encrypting and writing the disconnect.
 */
export function disconnectIndependentWorker(worker: Worker): void {
  worker.postMessage({ c: 'disconnect' } satisfies UiCommand);
  setTimeout(() => worker.terminate(), ADVANCED_DISCONNECT_FLUSH_MS);
}
