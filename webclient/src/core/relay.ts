import { ConnType, RendezvousMessage, RequestRelay } from '../gen/rendezvous';

export function buildRequestRelay(o: {
  licenceKey: string;
  peerId: string;
  uuid: string;
  connType?: ConnType;
}): Uint8Array {
  return RendezvousMessage.encode({
    union: {
      $case: 'request_relay',
      request_relay: RequestRelay.fromPartial({
        licence_key: o.licenceKey,
        id: o.peerId,
        uuid: o.uuid,
        conn_type: o.connType ?? ConnType.DEFAULT_CONN,
      }),
    },
  }).finish();
}
