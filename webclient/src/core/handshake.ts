import { IdPk, Message, PublicKey } from '../gen/message';
import { signOpen, sealSymmetricKey } from './crypto';

export function decodeIdPk(signed: Uint8Array, ed25519Pk: Uint8Array): { id: string; pk: Uint8Array } {
  const idpk = IdPk.decode(signOpen(signed, ed25519Pk));
  return { id: idpk.id, pk: idpk.pk };
}

// Trust link 1: RelayResponse.pk is IdPk{peerId, peerEd25519Pk} signed by the SERVER key.
export function verifyServerRelayPk(
  rrPk: Uint8Array,
  serverEd25519Pk: Uint8Array,
  expectPeerId: string,
): Uint8Array {
  const { id, pk } = decodeIdPk(rrPk, serverEd25519Pk);
  if (id !== expectPeerId) {
    throw new Error(`server-signed peer id mismatch: expected "${expectPeerId}", got "${id}"`);
  }
  return pk;
}

// Trust link 2: SignedId.id is IdPk{id, curve25519 box pk} signed by the PEER key.
export function verifyPeerSignedId(
  signedId: Uint8Array,
  peerEd25519Pk: Uint8Array,
): { id: string; boxPk: Uint8Array } {
  const { id, pk } = decodeIdPk(signedId, peerEd25519Pk);
  return { id, boxPk: pk };
}

export function buildPublicKeyMessage(peerBoxPk: Uint8Array): { bytes: Uint8Array; key: Uint8Array } {
  const { key, sealed, ourBoxPk } = sealSymmetricKey(peerBoxPk);
  const bytes = Message.encode({
    union: {
      $case: 'public_key',
      public_key: PublicKey.fromPartial({ asymmetric_value: ourBoxPk, symmetric_value: sealed }),
    },
  }).finish();
  return { bytes, key };
}
