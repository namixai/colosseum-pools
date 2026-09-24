// Getting the browser wallet onto this chain, decided by the wallet's own state rather than by
// the shape of its refusals. Kept out of chain.js and away from the browser globals so a test
// can drive it with a wallet that answers however a real one does.

/** The chain the wallet is on, lower case, as `eth_chainId` gives it. */
export async function chainOf(eth) {
  return String(await eth.request({ method: "eth_chainId" })).toLowerCase();
}

/** A refusal the person made, not the wallet: MetaMask's 4001, ethers' own name for it. */
export function cancelled(err) {
  return Boolean(err) && (Number(err.code) === 4001 || err.code === "ACTION_REJECTED");
}

/**
 * Leaves the wallet on `config.chainHex`, or throws saying what to add by hand.
 *
 * A wallet that does not know this chain refuses the switch, and what it refuses with is not
 * the same everywhere: MetaMask answers 4902, and Rabby answered "Unrecognized chain ID" under
 * another code -- measured on the live demo, 24 Sep 2026, where that difference stopped a
 * visitor at the first step with an alert and nothing to do about it. So the refusal decides
 * nothing here; what decides is which chain the wallet is on afterwards, read again each time.
 * The one refusal that is an answer rather than a limitation is the person cancelling.
 */
export async function ensureChain(eth, config) {
  const want = String(config.chainHex).toLowerCase();
  if ((await chainOf(eth)) === want) return "already";

  try {
    await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: config.chainHex }] });
  } catch (err) {
    if (cancelled(err)) throw err;
  }
  if ((await chainOf(eth)) === want) return "switched";

  await eth.request({
    method: "wallet_addEthereumChain",
    params: [{
      chainId: config.chainHex,
      chainName: config.chainName,
      nativeCurrency: config.nativeCurrency,
      rpcUrls: [config.rpc],
    }],
  });
  if ((await chainOf(eth)) === want) return "added";

  // Adding it went through and the wallet still is not on it: say what to type in by hand
  // rather than leave the visitor with a refusal they cannot act on.
  throw new Error(`This wallet is not on ${config.chainName}. Add it by hand: network name `
    + `${config.chainName}, RPC URL ${config.rpc}, chain id ${config.chainId}, currency symbol `
    + `${config.nativeCurrency.symbol}.`);
}
