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
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import hl
from .chain import JsonRpcReader
from .checks import ChainReader, GatewayError, NonceBook, Request, check
from .signer import SignerClient

MAX_BODY = 64 * 1024
DEFAULT_RPC = "https://rpc.hyperliquid-testnet.xyz/evm"


class Gateway:
    def __init__(self, reader: ChainReader, signer: SignerClient, submit=hl.submit, clock=time.time):
        self.reader = reader
        self.signer = signer
        self.submit = submit
        self.clock = clock
        self.nonces = NonceBook()

    def handle_order(self, body: Any) -> tuple[int, dict]:
        try:
            req = Request.from_json(body)
            cleared = check(req, self.reader, int(self.clock() * 1000), self.nonces)
            if not self.signer.has_key(cleared.key):
                raise GatewayError(503, "key_not_configured", cleared.key)

            action = req.action()
            signed = self.signer.sign(cleared.key, req.kind, action, req.nonce)
            base = {"account": req.account, "trader": cleared.trader, "key": cleared.key, "receipt": signed.receipt}
            if signed.http_status != 200 or not signed.signature:
                return 403 if signed.http_status == 403 else 502, {
                    **base, "status": "refused_by_signer", "signer": signed.body,
                }

            recovered = hl.recover_signer(action, signed.signature, req.nonce)
            if recovered.lower() != cleared.key.lower():
                return 502, {**base, "status": "signature_mismatch", "recovered": recovered}

            venue = self.submit(action, req.nonce, signed.signature)
            return 200, {**base, "status": "submitted", "venue": venue}
        except GatewayError as e:
            return e.status, {"status": "refused_by_gateway", "code": e.code, "detail": e.detail}


def make_handler(gw: Gateway, allow_origin: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "colosseum-pools-gateway"

        def _cors(self) -> None:
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Vary", "Origin")

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

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
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"status": "refused_by_gateway", "code": "bad_size"})
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, {"status": "refused_by_gateway", "code": "bad_json"})
                return
            status, payload = gw.handle_order(body)
            print(json.dumps({"t": int(time.time()), "http": status, **{k: payload.get(k) for k in
                  ("status", "code", "account", "trader", "key")}}), flush=True)
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
    server = ThreadingHTTPServer((host, int(port)), make_handler(Gateway(reader, signer), allow_origin))
    print(f"gateway listening on {host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
