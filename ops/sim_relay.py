"""The relayer of purchases by signature on simulated pools: a trader signs a USDC transfer of an
exam's price to the pool (EIP-3009 ReceiveWithAuthorization) and needs no gas; this sends
`SimPool.buyWithAuthorization` from the relayer's key, which pays it (docs/SIM-POOL.md).

    spike/.venv/bin/python -m ops.sim_relay --deployment sim1 --signed buy.json --dry-run
    spike/.venv/bin/python -m ops.sim_relay --deployment sim1 --signed buy.json --wallet relayer
    spike/.venv/bin/python -m ops.sim_relay --deployment sim1 --serve 8790 --wallet relayer   # POST /buy

A signed purchase is JSON: {"pool", "trader", "line", "valid_after", "valid_before", "nonce", "v",
"r", "s"}. Before it spends gas the relayer checks that the pool is one of the deployment's factory's,
the line is on its menu, the signature is not past its time and its nonce is unused, and that the
pool would take the call (eth_estimateGas). The token itself checks the signature, the amount (the
line's price) and that only that pool, its payee, can use it. The server listens on 127.0.0.1 only.
Testnet only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from eth_utils import keccak, to_checksum_address

from ops import deployments
from ops import keeper as core
from ops.contract_errors import refusal
from spike.hlspike import common as c

log = core.log
SIG = "buyWithAuthorization(address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
TYPES = ["address", "uint256", "uint256", "uint256", "bytes32", "uint8", "bytes32", "bytes32"]
EXAM_SOLD = "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64,uint256,bool)").hex()
# A signature that ends sooner than this is not worth the gas: it may lapse before the block.
MIN_LEFT_S = 30


class Refused(Exception):
    """A purchase the relayer will not send, and why."""


def parsed(raw: dict) -> dict:
    """A signed purchase, checked for shape: addresses, numbers, 32-byte words."""
    try:
        out = {"pool": to_checksum_address(raw["pool"]), "trader": to_checksum_address(raw["trader"]),
               "line": int(raw["line"]), "valid_after": int(raw["valid_after"]),
               "valid_before": int(raw["valid_before"]), "v": int(raw["v"])}
        for k in ("nonce", "r", "s"):
            word = bytes.fromhex(str(raw[k]).removeprefix("0x"))
            if len(word) != 32:
                raise ValueError(f"{k} is not 32 bytes")
            out[k] = word
    except (KeyError, ValueError, TypeError) as exc:
        raise Refused(f"malformed: {exc}") from exc
    if out["v"] not in (27, 28):
        raise Refused("malformed: v is 27 or 28")
    return out


def check(p: dict, now: int, pools: list[str], menu_len: int, used: bool) -> None:
    """Refuses a purchase not worth sending: another factory's pool, no such line, past its time, a
    used nonce."""
    if p["pool"] not in pools:
        raise Refused("not a pool of this deployment's factory")
    if not 0 <= p["line"] < menu_len:
        raise Refused("no such line on the pool's menu")
    if p["valid_before"] < now + MIN_LEFT_S:
        raise Refused("the signature ends too soon")
    if used:
        raise Refused("the nonce is used")


def args_of(p: dict) -> list:
    return [p["trader"], p["line"], p["valid_after"], p["valid_before"], p["nonce"], p["v"], p["r"], p["s"]]


class Relay:
    def __init__(self, record: dict, wallet, sender: str, dry: bool):
        self.factory = to_checksum_address(record["SimPoolFactory"])
        self.usdc = to_checksum_address(record["usdc"])
        self.wallet = wallet
        self.sender = to_checksum_address(sender)
        self.dry = dry

    def relay(self, raw: dict) -> dict:
        p = parsed(raw)
        pools = [to_checksum_address(a) for a in core.view(self.factory, "pools()", "address[]")]
        menu_len = len(core.view(p["pool"], "menu()", "(uint64,uint64)[]")) if p["pool"] in pools else 0
        used = bool(core.view(self.usdc, "authorizationState(address,bytes32)", "bool", ["address", "bytes32"],
                              [p["trader"], p["nonce"]]))
        now = int(c.rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
        check(p, now, pools, menu_len, used)
        data = c.encode_call(SIG, TYPES, args_of(p))
        try:
            c.rpc("eth_estimateGas", [{"from": self.sender, "to": p["pool"], "data": "0x" + data.hex()}])
        except RuntimeError as exc:
            raise Refused(f"the pool refuses it: {refusal(exc)}") from exc
        if self.dry:
            log("would_relay", pool=p["pool"], trader=p["trader"], line=p["line"])
            return {"sent": False, "pool": p["pool"], "trader": p["trader"], "line": p["line"]}
        rcpt = c.transact(self.wallet, p["pool"], SIG, TYPES, args_of(p))
        exam = next((to_checksum_address("0x" + l["topics"][1][-40:]) for l in rcpt["logs"]
                     if l["topics"] and l["topics"][0] == EXAM_SOLD), None)
        log("relayed", pool=p["pool"], trader=p["trader"], line=p["line"], exam=exam, tx=rcpt["transactionHash"])
        return {"sent": True, "exam": exam, "tx": rcpt["transactionHash"]}


def serve(relay: Relay, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server's name)
            if self.path != "/buy":
                self.send_error(404)
                return
            try:
                length = max(0, int(self.headers.get("Content-Length", 0)))
                body = json.loads(self.rfile.read(min(length, 4096)))
                out, code = relay.relay(body), 200
            except (Refused, ValueError) as exc:  # the buyer's request, not the node (review of cp#263)
                out, code = {"refused": str(exc)}, 400
            except Exception as exc:  # the node failed; the buyer may retry
                out, code = {"error": str(exc)[:200]}, 502
            payload = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # the relayer logs its own lines
            pass

    log("relay_listening", port=port)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True)
    p.add_argument("--wallet", default="relayer", help="a funded testnet key that pays the gas")
    p.add_argument("--signed", help="a signed purchase, as JSON")
    p.add_argument("--serve", type=int, metavar="PORT", help="listen on 127.0.0.1:PORT for POST /buy")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from", dest="sender", help="address the call is tried from in a dry run")
    args = p.parse_args(argv)
    c.assert_testnet()
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    wallet = None if args.dry_run else c.account(args.wallet)
    relay = Relay(record, wallet, wallet.address if wallet else (args.sender or c.address_of(args.wallet)), args.dry_run)
    if args.serve:
        serve(relay, args.serve)
        return 0
    if not args.signed:
        p.error("--signed or --serve")
    try:
        print(json.dumps(relay.relay(json.loads(pathlib.Path(args.signed).read_text()))))
    except Refused as exc:
        print(json.dumps({"refused": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
