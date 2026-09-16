// A minimal CBOR reader, enough to open a Nitro attestation document (COSE_Sign1) and read
// fields out of its signed payload. It does not check the signature; that is a separate step.

export function decode(bytes) {
  let i = 0;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);

  function length(info) {
    if (info < 24) return info;
    if (info === 24) return bytes[i++];
    if (info === 25) { const v = view.getUint16(i); i += 2; return v; }
    if (info === 26) { const v = view.getUint32(i); i += 4; return v; }
    if (info === 27) { const v = view.getBigUint64(i); i += 8; return Number(v); }
    throw new Error(`unsupported CBOR length ${info}`);
  }

  const BREAK = 0xff;
  const atBreak = () => bytes[i] === BREAK && (i++, true);

  function chunks(major) {
    // Indefinite-length string: definite-length chunks of the same major type, then a break.
    const parts = [];
    while (!atBreak()) {
      const head = bytes[i++];
      if (head >> 5 !== major || (head & 0x1f) === 31) throw new Error("bad chunk in an indefinite string");
      const n = length(head & 0x1f);
      parts.push(bytes.slice(i, i + n));
      i += n;
    }
    const out = new Uint8Array(parts.reduce((a, p) => a + p.length, 0));
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  function item() {
    const head = bytes[i++];
    const major = head >> 5;
    const info = head & 0x1f;
    const indefinite = info === 31 && major >= 2 && major <= 5;
    switch (major) {
      case 0: return length(info);
      case 1: return -1 - length(info);
      case 2: {
        if (indefinite) return chunks(2);
        const n = length(info); const out = bytes.slice(i, i + n); i += n; return out;
      }
      case 3: {
        if (indefinite) return new TextDecoder().decode(chunks(3));
        const n = length(info); const out = new TextDecoder().decode(bytes.slice(i, i + n)); i += n; return out;
      }
      case 4: {
        const out = [];
        if (indefinite) { while (!atBreak()) out.push(item()); return out; }
        const n = length(info);
        for (let k = 0; k < n; k++) out.push(item());
        return out;
      }
      case 5: {
        const out = new Map();
        if (indefinite) { while (!atBreak()) { const key = item(); out.set(key, item()); } return out; }
        const n = length(info);
        for (let k = 0; k < n; k++) { const key = item(); out.set(key, item()); }
        return out;
      }
      case 6: length(info); return item(); // tag: keep the tagged value
      case 7:
        if (info === 20) return false;
        if (info === 21) return true;
        if (info === 22) return null;
        throw new Error(`unsupported CBOR simple value ${info}`);
      default:
        throw new Error(`unsupported CBOR major type ${major}`);
    }
  }

  const value = item();
  if (i !== bytes.length) throw new Error("trailing bytes after the CBOR item");
  return value;
}

export function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function fromBase64(text) {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let k = 0; k < bin.length; k++) out[k] = bin.charCodeAt(k);
  return out;
}

/** Opens a COSE_Sign1 Nitro attestation document and returns its signed payload fields. */
export function attestationPayload(docB64) {
  const cose = decode(fromBase64(docB64));
  if (!Array.isArray(cose) || cose.length !== 4) throw new Error("not a COSE_Sign1 structure");
  const payload = decode(cose[2]);
  const pcrs = payload.get("pcrs");
  return {
    moduleId: payload.get("module_id"),
    timestamp: payload.get("timestamp"),
    pcr0: pcrs ? hex(pcrs.get(0)) : null,
    nonce: payload.get("nonce") ? hex(payload.get("nonce")) : null,
  };
}
