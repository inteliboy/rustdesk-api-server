// RustDesk AddrMangle: ip:port obfuscated with a microsecond timestamp.
// Unused on the relay-only path; kept for protocol completeness.
//
// v4 layout (u128, little-endian, trailing zero bytes stripped):
//   bits [49..)  : ipLE32 + tm      (ip as LE u32 of the octets)
//   bits [17..49): tm               (unix micros truncated to u32)
//   bits [0..17) : port + (tm & 0xFFFF)
// v6 layout: 16 address bytes || port as LE u16 (18 bytes, no obfuscation).

export type SocketAddr = { ip: string; port: number };

const MASK128 = (1n << 128n) - 1n;

function parseV4(ip: string): number[] {
  const parts = ip.split('.');
  if (parts.length !== 4) throw new Error(`bad ipv4: ${ip}`);
  return parts.map((p) => {
    const n = Number(p);
    if (!/^\d+$/.test(p) || n > 255) throw new Error(`bad ipv4: ${ip}`);
    return n;
  });
}

function parseV6(ip: string): Uint8Array {
  const halves = ip.split('::');
  if (halves.length > 2) throw new Error(`bad ipv6: ${ip}`);
  const toGroups = (s: string): number[] =>
    s === '' ? [] : s.split(':').map((g) => {
      if (!/^[0-9a-fA-F]{1,4}$/.test(g)) throw new Error(`bad ipv6: ${ip}`);
      return parseInt(g, 16);
    });
  const head = toGroups(halves[0]!);
  const tail = halves.length === 2 ? toGroups(halves[1]!) : [];
  const missing = 8 - head.length - tail.length;
  if (halves.length === 1 ? head.length !== 8 : missing < 1) throw new Error(`bad ipv6: ${ip}`);
  const groups = halves.length === 1 ? head : [...head, ...new Array<number>(missing).fill(0), ...tail];
  const out = new Uint8Array(16);
  groups.forEach((g, i) => {
    out[i * 2] = g >> 8;
    out[i * 2 + 1] = g & 0xff;
  });
  return out;
}

function formatV6(bytes: Uint8Array): string {
  const groups: string[] = [];
  for (let i = 0; i < 16; i += 2) groups.push((((bytes[i]! << 8) | bytes[i + 1]!) >>> 0).toString(16));
  return groups.join(':');
}

export function encodeAddr(addr: SocketAddr, nowMicros?: bigint): Uint8Array {
  if (addr.port < 0 || addr.port > 0xffff || !Number.isInteger(addr.port)) {
    throw new Error(`bad port: ${addr.port}`);
  }
  if (addr.ip.includes(':')) {
    const out = new Uint8Array(18);
    out.set(parseV6(addr.ip), 0);
    out[16] = addr.port & 0xff;
    out[17] = addr.port >> 8;
    return out;
  }
  const o = parseV4(addr.ip);
  const ip = BigInt((o[0]! | (o[1]! << 8) | (o[2]! << 16) | (o[3]! << 24)) >>> 0);
  const tm = (nowMicros ?? BigInt(Date.now()) * 1000n) & 0xffffffffn;
  const v = (((ip + tm) << 49n) | (tm << 17n) | (BigInt(addr.port) + (tm & 0xffffn))) & MASK128;
  const bytes = new Uint8Array(16);
  let x = v;
  for (let i = 0; i < 16; i++) {
    bytes[i] = Number(x & 0xffn);
    x >>= 8n;
  }
  let len = 16;
  while (len > 0 && bytes[len - 1] === 0) len--;
  return bytes.slice(0, len);
}

export function decodeAddr(bytes: Uint8Array): SocketAddr {
  if (bytes.length > 16) {
    if (bytes.length !== 18) throw new Error(`bad mangled addr length: ${bytes.length}`);
    return { ip: formatV6(bytes.subarray(0, 16)), port: bytes[16]! | (bytes[17]! << 8) };
  }
  let n = 0n;
  for (let i = bytes.length - 1; i >= 0; i--) n = (n << 8n) | BigInt(bytes[i]!);
  const tm = (n >> 17n) & 0xffffffffn;
  const ip = ((n >> 49n) - tm) & 0xffffffffn;
  const port = ((n & 0xffffffn) - (tm & 0xffffn)) & 0xffffn;
  const b0 = Number(ip & 0xffn);
  const b1 = Number((ip >> 8n) & 0xffn);
  const b2 = Number((ip >> 16n) & 0xffn);
  const b3 = Number((ip >> 24n) & 0xffn);
  return { ip: `${b0}.${b1}.${b2}.${b3}`, port: Number(port) };
}
