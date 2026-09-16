"""Pool gateway HTTP server.

    GATEWAY_FACTORY=0x… GATEWAY_REGISTRY=0x… SIGNER_URL=https://… SIGNER_TOKENS_FILE=~/… \\
        spike/.venv/bin/python -m gateway.server

POST /v1/order   one order or one cancel, see docs/GATEWAY.md
GET  /v1/health  liveness and configuration summary (no secrets)

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import hl
from .chain import JsonRpcReader
from .checks import ChainReader, GatewayError, NonceBook, Request, check
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


class Gateway:
    def __init__(self, reader: ChainReader, signer: SignerClient, submit=hl.submit, clock=time.time):
        self.reader = reader
        self.signer = signer
        self.submit = submit
        self.clock = clock
        self.nonces = NonceBook()

    def handle_order(self, body: Any) -> tuple[int, dict]:
        try:
            return self._handle(body)
        except GatewayError as e:
            return e.status, {"status": "refused_by_gateway", "code": e.code, "detail": e.detail}
        except Exception as e:  # a chain read or the Signer failed; nothing was submitted
            log_line(event="upstream_failed", error=type(e).__name__)
            return 502, {"status": "gateway_error", "code": "upstream_failed"}

    def _handle(self, body: Any) -> tuple[int, dict]:
        req = Request.from_json(body)
        cleared = check(req, self.reader, int(self.clock() * 1000), self.nonces)
        if not self.signer.has_key(cleared.key):
            raise GatewayError(503, "key_not_configured", cleared.key)

        action = req.action()
        signed = self.signer.sign(cleared.key, req.kind, action, req.nonce)
        base = {"account": req.account, "trader": cleared.trader, "key": cleared.key, "receipt": signed.receipt}
        if signed.http_status != 200 or not signed.signature:
            return 403 if signed.http_status == 403 else 502, {
                **base, "status": "refused_by_signer", "signer": signer_reason(signed.body),
            }

        signature = signature_parts(signed.signature)
        if signature is None:
            return 502, {**base, "status": "bad_signer_response"}
        recovered = hl.recover_signer(action, signature, req.nonce)
        if recovered.lower() != cleared.key.lower():
            return 502, {**base, "status": "signature_mismatch", "recovered": recovered}

        try:
            venue = self.submit(action, req.nonce, signature)
        except Exception as e:
            log_line(event="venue_unreachable", error=type(e).__name__)
            return 502, {**base, "status": "venue_unreachable",
                         "detail": "the order may or may not have reached Hyperliquid; check the account"}
        return 200, {**base, "status": "submitted", "venue": venue}


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


def make_handler(gw: Gateway, allow_origin: str = "", request_timeout: float = REQUEST_TIMEOUT_S):
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
                self._send(200, {"ok": True, "chain_id": 998})
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


def main() -> int:
    rpc = os.environ.get("GATEWAY_RPC_URL", DEFAULT_RPC)
    reader = JsonRpcReader(rpc, os.environ["GATEWAY_FACTORY"], os.environ["GATEWAY_REGISTRY"])
    reader.check_chain()
    signer = SignerClient(os.environ["SIGNER_URL"], SignerClient.load_tokens(os.environ["SIGNER_TOKENS_FILE"]))
    host, _, port = os.environ.get("GATEWAY_BIND", "127.0.0.1:8787").partition(":")
    allow_origin = os.environ.get("GATEWAY_ALLOW_ORIGIN", "")  # the app's origin, if it calls from a browser
    server = BoundedServer((host, int(port)), make_handler(Gateway(reader, signer), allow_origin))
    print(f"gateway listening on {host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
