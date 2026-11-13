// Keccak-256 over 0x-hex bytes, for the tests alone (the page has ethers.keccak256): the sponge of FIPS 202 with
// Keccak's own padding, 1600-bit state in 64-bit lanes. The same as app/tests/simpool.test.mjs's, held there to
// vectors from `cast keccak`; held here to `cast calldata` (app/tests/operator.test.mjs).
const RC = [0x1n, 0x8082n, 0x800000000000808an, 0x8000000080008000n, 0x808bn, 0x80000001n, 0x8000000080008081n,
  0x8000000000008009n, 0x8an, 0x88n, 0x80008009n, 0x8000000an, 0x8000808bn, 0x800000000000008bn, 0x8000000000008089n,
  0x8000000000008003n, 0x8000000000008002n, 0x8000000000000080n, 0x800an, 0x800000008000000an, 0x8000000080008081n,
  0x8000000000008080n, 0x80000001n, 0x8000000080008008n];
const ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39, 41, 45, 15, 21, 8, 18, 2, 61, 56, 14];
const M64 = (1n << 64n) - 1n;
const rotl = (x, n) => (n === 0 ? x : ((x << BigInt(n)) | (x >> BigInt(64 - n))) & M64);
export function keccak256(hex) {
  const bytes = Buffer.from(hex.slice(2), "hex");
  const rate = 136;
  const padded = Buffer.alloc(Math.ceil((bytes.length + 1) / rate) * rate);
  bytes.copy(padded);
  padded[bytes.length] ^= 0x01;
  padded[padded.length - 1] ^= 0x80;
  const a = new Array(25).fill(0n);
  for (let off = 0; off < padded.length; off += rate) {
    for (let i = 0; i < rate / 8; ++i) a[i] ^= padded.readBigUInt64LE(off + 8 * i);
    for (let round = 0; round < 24; ++round) {
      const c = [0, 1, 2, 3, 4].map((x) => a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20]);
      for (let x = 0; x < 5; ++x) {
        const d = c[(x + 4) % 5] ^ rotl(c[(x + 1) % 5], 1);
        for (let y = 0; y < 25; y += 5) a[x + y] ^= d;
      }
      const b = new Array(25);
      for (let x = 0; x < 5; ++x) {
        for (let y = 0; y < 5; ++y) b[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(a[x + 5 * y], ROT[x + 5 * y]);
      }
      for (let y = 0; y < 25; y += 5) {
        for (let x = 0; x < 5; ++x) a[x + y] = b[x + y] ^ (~b[((x + 1) % 5) + y] & M64 & b[((x + 2) % 5) + y]);
      }
      a[0] ^= RC[round];
    }
  }
  const out = Buffer.alloc(32);
  for (let i = 0; i < 4; ++i) out.writeBigUInt64LE(a[i], 8 * i);
  return `0x${out.toString("hex")}`;
}
