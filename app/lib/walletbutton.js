// The "Connect wallet" button: what a click does, and what a change of account does.
//
// chain.connect() tells every onAccount listener about the new address, and the listener redraws the page. The
// click used to redraw it a second time. Two renders of a page start their reads in the same moment, ethers sends
// them as one JSON-RPC batch, and on the pool page that was 22 calls: the public RPC refuses a batch over 20 with
// -32010, and the page the visitor was about to buy from failed to load (found on the live site, 1 Oct 2026).
// One render per connection: the listener draws, the click only connects.

/** Wires the button. `connect` resolves once the listeners have run; `report` shows an error to the visitor. */
export function wireWallet({ button, connect, onAccount, paint, route, report }) {
  button.addEventListener("click", async () => {
    try {
      await connect();
    } catch (err) {
      paint(null);
      report(err);
    }
  });
  onAccount((addr) => {
    paint(addr);
    route();
  });
}
