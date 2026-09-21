// Browser stub for Node's `stream`, for jMuxer only.
//
// jMuxer imports `stream` at module scope but uses it in exactly one place:
// createStream(), an opt-in API that wraps feed() in a Duplex. This client
// calls feed() directly, so the Duplex is never constructed — the import is
// the only reason a bundler needs `stream` to exist at all.
//
// Bundling a full stream polyfill to satisfy a code path we never take would
// add real weight for nothing. Constructing it throws rather than returning a
// broken object, so if anything ever does reach createStream() it fails
// loudly here instead of misbehaving somewhere downstream.
export class Duplex {
  constructor() {
    throw new Error(
      'node stream is not available in the browser build; jMuxer.createStream() is unsupported — feed() directly',
    );
  }
}

export default { Duplex };
