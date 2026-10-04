"""Pool gateway HTTP server.

    GATEWAY_FACTORY=0x… GATEWAY_REGISTRY=0x… GATEWAY_SIGNER=demo GATEWAY_KEYS_DIR=~/… \\
        spike/.venv/bin/python -m gateway.server

GATEWAY_SIGNER picks who signs. `demo`, what the demo runs: the gateway signs with testnet
keys from GATEWAY_KEYS_DIR and checks the platform's caps before it signs (gateway/demo_signer.py).
`signer`: it asks a Usenami Signer gateway (SIGNER_URL, SIGNER_TOKENS_FILE); the demo doesn't
use this mode.

POST /v1/order   one order, one cancel, or moving the account's stop or take, see docs/GATEWAY.md
GET  /v1/health  liveness and configuration summary (no secrets)

Before an order that may open or grow a position goes, the gateway puts a stop at the pool's
rule line and a take at the target on Hyperliquid itself (gateway/protect.py), and a sweep every
GATEWAY_PROTECT_EVERY seconds (default 15) keeps them there.

Testnet only: refuses to start unless the RPC reports chain 998, and submits only to
Hyperliquid's testnet API.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import hl
from .chain import JsonRpcReader, Throttled
from .checks import ChainReader, GatewayError, NonceBook, Request, check
from .demo_signer import DemoSigner
from .protect import Protector
from .signer import SignerClient

MAX_BODY = 64 * 1024
DEFAULT_RPC = "https://rpc.hyperliquid-testnet.xyz/evm"
# A client that stops sending gets its connection closed after this long, and only this many
# connections are served at once; the rest get a 503 straight away.
REQUEST_TIMEOUT_S = 10.0
MAX_ACTIVE = 32
HEX_WORD = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
# What a refusal may pass on from the Signer's answer, besides the signed receipt.
SIGNER_REASON_FIELDS = ("error", "reason", "code", "message")
# 15 and not 10: the keeper, the sweep and the orders share Hyperliquid's 1,200 of weight a minute per IP,
# and at 10 two accounts trading at nginx's rate already spent 1,202 (docs/GATEWAY.md, "The host's minute").
PROTECT_EVERY_S = 15.0


def log_line(**fields) -> None:
    print(json.dumps({"t": int(time.time()), **fields}, default=str), flush=True)


def signature_parts(sig: Any) -> dict | None:
    """The {r, s, v} Hyperliquid expects, or None if the Signer sent something else."""
    if not isinstance(sig, dict):
        return None
    r, s, v = sig.get("r"), sig.get("s"), sig.get("v")
    if not (isinstance(r, str) and HEX_WORD.match(r) and isinstance(s, str) and HEX_WORD.match(s)):
        return None
    if type(v) is not int or v not in (27, 28):
        return None
    return {"r": r, "s": s, "v": v}


def signer_reason(body: Any) -> dict:
    if not isinstance(body, dict):
        return {}
    return {k: str(body[k])[:200] for k in SIGNER_REASON_FIELDS if k in body}


def signer_from_env(env) -> tuple[str, DemoSigner | SignerClient]:
    mode = env.get("GATEWAY_SIGNER")
    if mode == "demo":
        return mode, DemoSigner(DemoSigner.load_keys(env["GATEWAY_KEYS_DIR"]))
    if mode == "signer":
        return mode, SignerClient(env["SIGNER_URL"], SignerClient.load_tokens(env["SIGNER_TOKENS_FILE"]))
    raise SystemExit("GATEWAY_SIGNER must be demo or signer")


class Gateway:
    def __init__(self, reader: ChainReader, signer: DemoSigner | SignerClient, submit=hl.submit, clock=time.time,
                 venue=None):
        self.reader = reader
        self.signer = signer
        self.submit = submit
        self.clock = clock
        self.nonces = NonceBook()
        self._own_nonce = 0
        self._own_nonce_lock = threading.Lock()
        self.protector = Protector(reader, venue if venue is not None else hl.Venue(), self._act, clock)

    def handle_order(self, body: Any) -> tuple[int, dict]:
        try:
            return self._handle(body)
        except GatewayError as e:
            return e.status, {"status": "refused_by_gateway", "code": e.code, "detail": e.detail}
        except Throttled:
            # The node is rate limiting us, which is not the same as being broken: waiting
            # helps, and the trader can be told so. 429 and not a 5xx on purpose -- the CDN in
            # front of this replaces the body of a 5xx with its own page (measured 24 Sep 2026,
            # a trader's order came back as Cloudflare's HTML), and then nothing this gateway
            # said reaches anyone.
            log_line(event="upstream_busy")
            return 429, {"status": "busy", "code": "upstream_busy",
                         "detail": "the chain node is refusing reads right now; try again in a moment"}
        except Exception as e:  # a chain read or the Signer failed; nothing was submitted
            log_line(event="upstream_failed", error=type(e).__name__)
            return 502, {"status": "gateway_error", "code": "upstream_failed"}

    def _handle(self, body: Any) -> tuple[int, dict]:
        req = Request.from_json(body)
        cleared = check(req, self.reader, int(self.clock() * 1000), self.nonces)
        # One request at a time per account, from the stop and take placed for it to Hyperliquid's
        # answer: a sweep, or a second order for the same account, in between would read a book
        # without this order and protect the account for less than it is about to hold.
        with self.protector.holding(req.account):
            return self._handle_held(req, cleared)

    def _handle_held(self, req: Request, cleared) -> tuple[int, dict]:
        # check() claimed the nonce, so a copy of this request arriving meanwhile is refused
        # without a signer call. Until a signature for the right key exists, nothing can
        # reach Hyperliquid, and any failure gives the nonce back for a retry.
        protection = None
        try:
            if req.kind in ("stop", "take"):
                action = self.protector.move(req.account, cleared.key, "sl" if req.kind == "stop" else "tp",
                                             req.asset, Decimal(req.message["triggerPx"]))
                signature, status, payload = self._sign(req, cleared, action, "protect")
            else:
                if req.kind == "cancel":
                    self.protector.refuse_protective_cancel(req.account, req.message["oid"])
                action = req.action()
                signature, status, payload = self._sign(req, cleared, action, req.kind)
                # The stop and the take go first: an order that may open a position is never
                # submitted without them on the book.
                if signature is not None and req.opens():
                    protection = self.protector.before_opening(
                        req.account, cleared.key, req.asset, req.message["isBuy"], Decimal(req.message["size"]),
                        avoid_nonce=req.nonce)
        except BaseException:
            self.nonces.release(cleared.trader, req.nonce)
            raise
        if signature is None:
            self.nonces.release(cleared.trader, req.nonce)
            return status, payload

        # From here the nonce stays spent: the venue may have the order even if the call fails.
        base = payload if protection is None else {**payload, "protection": protection}
        try:
            venue = self.submit(action, req.nonce, signature)
        except Exception as e:
            self._trader_asked(req)  # it may have reached Hyperliquid all the same
            log_line(event="venue_unreachable", error=type(e).__name__)
            return 502, {**base, "status": "venue_unreachable",
                         "detail": "the order may or may not have reached Hyperliquid; check the account"}
        refusal, confirmed = hl.venue_outcome(venue)
        if refusal is not None:
            return 422, {**base, "status": "refused_by_venue", "reason": refusal, "venue": venue}
        self._trader_asked(req)  # confirmed, or an answer that confirms nothing: either way it may stand
        if not confirmed:
            return 502, {**base, "status": "venue_unconfirmed", "venue": venue,
                         "detail": "Hyperliquid's answer confirms nothing; check the account"}
        return 200, {**base, "status": "submitted", "venue": venue}

    def _trader_asked(self, req: Request) -> None:
        """A take the trader asked to move is theirs from here on, unless Hyperliquid refused it."""
        if req.kind == "take":
            self.protector.trader_asked(req.account, req.asset, Decimal(req.message["triggerPx"]))

    def _sign(self, req: Request, cleared, action: dict, kind: str) -> tuple[dict | None, int, dict]:
        """Asks the signer and checks its answer. Returns the signature only if it is
        Hyperliquid-shaped and recovers to the account's key; otherwise the answer to send.
        The demo signer refuses an order over the caps with a GatewayError."""
        if not self.signer.has_key(cleared.key):
            raise GatewayError(503, "key_not_configured", cleared.key)

        signed = self.signer.sign(cleared.key, kind, action, req.nonce)
        base = {"account": req.account, "trader": cleared.trader, "key": cleared.key, "receipt": signed.receipt}
        if signed.http_status != 200 or not signed.signature:
            return None, 403 if signed.http_status == 403 else 502, {
                **base, "status": "refused_by_signer", "signer": signer_reason(signed.body),
            }

        signature = signature_parts(signed.signature)
        if signature is None:
            return None, 502, {**base, "status": "bad_signer_response"}
        recovered = hl.recover_signer(action, signature, req.nonce)
        if recovered.lower() != cleared.key.lower():
            return None, 502, {**base, "status": "signature_mismatch", "recovered": recovered}
        return signature, 200, base

    def _next_nonce(self, avoid: int | None) -> int:
        """A nonce for an action of the gateway's own. Hyperliquid wants every nonce of a key
        used once, and the trader's nonce of the request in hand is taken already."""
        with self._own_nonce_lock:
            nonce = max(int(self.clock() * 1000), self._own_nonce + 1)
            if nonce == avoid:
                nonce += 1
            self._own_nonce = nonce
            return nonce

    def _act(self, key: str, action: dict, avoid_nonce: int | None = None) -> tuple[str | None, bool, Any]:
        """Signs the gateway's own stop or take with the account's key, checks the signature the
        way a trader's is checked, and submits it. Returns (why it failed, whether Hyperliquid
        confirmed it, Hyperliquid's answer)."""
        nonce = self._next_nonce(avoid_nonce)
        try:
            signed = self.signer.sign(key, "protect", action, nonce)
        except GatewayError as e:
            return f"the signer refused it: {e.code} {e.detail}", False, None
        signature = signature_parts(signed.signature) if signed.http_status == 200 else None
        if signature is None:
            return f"the signer refused it: {signer_reason(signed.body)}", False, None
        if hl.recover_signer(action, signature, nonce).lower() != key.lower():
            return "it was signed by another key", False, None
        try:
            venue = self.submit(action, nonce, signature)
        except Exception as e:
            return f"Hyperliquid was unreachable: {type(e).__name__}", False, None
        refusal, confirmed = hl.venue_outcome(venue)
        log_line(event="protect_sent", key=key, action=action.get("type"), refusal=refusal, confirmed=confirmed)
        return refusal, confirmed, venue


class BoundedServer(ThreadingHTTPServer):
    """Serves at most `max_active` connections at once and answers the rest with a 503."""

    daemon_threads = True
    BUSY = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

    def __init__(self, address, handler, max_active: int = MAX_ACTIVE):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_active)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(self.BUSY)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_handler(gw: Gateway, allow_origin: str = "", request_timeout: float = REQUEST_TIMEOUT_S,
                 health: dict | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "colosseum-pools-gateway"
        timeout = request_timeout  # applies to every read and write on the connection

        def _cors(self) -> None:
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Vary", "Origin")

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, default=str).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the client left before the answer

        def do_OPTIONS(self):  # noqa: N802  (browser preflight for the app)
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            if self.path == "/v1/health":
                self._send(200, {"ok": True, "chain_id": 998, **(health or {})})
            else:
                self._send(404, {"status": "not_found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/v1/order":
                self._send(404, {"status": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"status": "refused_by_gateway", "code": "bad_size"})
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, {"status": "refused_by_gateway", "code": "bad_json"})
                return
            status, payload = gw.handle_order(body)
            log_line(http=status, **{k: payload.get(k) for k in ("status", "code", "account", "trader", "key")})
            self._send(status, payload)

        def log_message(self, fmt, *args):  # the JSON line above is the log
            return

    return Handler


def health_summary(mode: str, signer: Any, protect_every: float) -> dict:
    """What `/v1/health` tells a client about this gateway.

    The per-order notional cap belongs to the SIGNER in use, not to this process. The demo signer
    holds one and reports it; the enclave enforces its own and does not tell us, so the field is
    ABSENT in that mode rather than carrying a number from here. A constant published here would
    be a guess about the enclave that clients would then size against.

    Why publish it at all: an agent that guesses high sends an order the signer rejects, and that
    refusal is not `busy`, so it costs the agent one of its few daily orders for a number it could
    have read (`agents/desk.py`, `Limits.order_cap`).
    """
    out: dict = {"signer": mode}
    if mode == "demo":
        out["keys"] = len(signer)
    cap = getattr(signer, "max_order_notional_usdc", None)
    if cap is not None:
        out["max_order_notional_usdc"] = float(cap)
    out["protect_every"] = protect_every
    return out


def main() -> int:
    rpc = os.environ.get("GATEWAY_RPC_URL", DEFAULT_RPC)
    reader = JsonRpcReader(rpc, os.environ["GATEWAY_FACTORY"], os.environ["GATEWAY_REGISTRY"])
    reader.check_chain()
    mode, signer = signer_from_env(os.environ)
    host, _, port = os.environ.get("GATEWAY_BIND", "127.0.0.1:8787").partition(":")
    allow_origin = os.environ.get("GATEWAY_ALLOW_ORIGIN", "")  # the app's origin, if it calls from a browser
    every = float(os.environ.get("GATEWAY_PROTECT_EVERY", PROTECT_EVERY_S))
    health = health_summary(mode, signer, every)
    gateway = Gateway(reader, signer)
    # Before the sweep starts: the accounts this signer's keys trade right now, so one with a position
    # open is swept from the first pass and not from its trader's next order.
    addresses = getattr(signer, "addresses", None)
    taken = gateway.protector.take_on(addresses()) if addresses else []
    log_line(event="protect_took_on", accounts=taken, keys_listed=addresses is not None)
    threading.Thread(target=gateway.protector.run, args=(every,), name="protect-sweep", daemon=True).start()
    server = BoundedServer((host, int(port)), make_handler(gateway, allow_origin, health=health))
    print(f"gateway listening on {host}:{port}, signer: {mode}, stop and take swept every {every:g} s", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
