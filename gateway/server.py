"""Pool gateway HTTP server.

    GATEWAY_FACTORY=0x… GATEWAY_REGISTRY=0x… GATEWAY_SIGNER=demo GATEWAY_KEYS_DIR=~/… \\
        spike/.venv/bin/python -m gateway.server

GATEWAY_SIGNER picks who signs. `demo`, what the demo runs: the gateway signs with testnet
keys from GATEWAY_KEYS_DIR and checks the platform's caps before it signs (gateway/demo_signer.py).
`signer`: it asks a Usenami Signer gateway (SIGNER_URL, SIGNER_TOKENS_FILE); the demo doesn't
use this mode.

POST /v1/order   one order, one cancel, or moving the account's stop or take, see docs/GATEWAY.md
POST /v1/faucet  one exam's dUSDC and its gas in HYPE for a tester, where the deployment has a faucet
                 (gateway/faucet.py)
GET  /v1/contacts?pool=0x…  the contacts a pool's owner left for its traders and co-investors, and
POST /v1/contacts           the owner's signed record that replaces them, where the gateway has a
                            directory for them (gateway/contacts.py)
GET  /v1/health  liveness and configuration summary (no secrets); `ready: false` while the gateway,
                 just started, still reads the accounts its keys trade

Before an order that may open or grow a position goes, the gateway puts a stop at the pool's
rule line and a take at the target on Hyperliquid itself (gateway/protect.py), and a sweep every
GATEWAY_PROTECT_EVERY seconds (default 30) keeps them there, every GATEWAY_PROTECT_HOT_EVERY
seconds (default 15) for an account where a position can stand without a stop.

One network, the one COLOSSEUM_NETWORK names (gateway/__init__.py): testnet when it is unset or
empty, mainnet when it says so, and no start on any other value. The gateway refuses to start unless
the RPC reports that network's chain (998 on testnet, 999 on mainnet), and submits only to that
network's Hyperliquid API. The demo signer and the faucet are testnet's alone: on another network
the gateway does not start with either (`refuse_testnet_parts`).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from spike.hlspike import network

from . import NETWORK, hl
from .weight import METER
from .chain import CHAIN_ID, JsonRpcReader, Throttled
from .contacts import Contacts, contacts_from_env
from .faucet import Faucet, faucet_from_env
from .checks import ChainReader, GatewayError, NonceBook, Request, check
from .demo_signer import DemoSigner, size_caps
from .protect import LONG, SHORT, Protector, agent_unknown, leverage_caps
from .signer import SignerClient

MAX_BODY = 64 * 1024
DEFAULT_RPC = NETWORK.rpc_url  # the network's public node; GATEWAY_RPC_URL names another
# A client that stops sending gets its connection closed after this long, and only this many
# connections are served at once; the rest get a 503 straight away.
REQUEST_TIMEOUT_S = 10.0
MAX_ACTIVE = 32
HEX_WORD = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
# What a refusal may pass on from the Signer's answer, besides the signed receipt.
SIGNER_REASON_FIELDS = ("error", "reason", "code", "message")
# 15 and not 10: the keeper, the sweep and the orders share Hyperliquid's 1,200 of weight a minute per IP,
# and at 10 two accounts trading at nginx's rate already spent 1,202 (docs/GATEWAY.md, "The host's minute").
PROTECT_EVERY_S = 30.0
# The shorter period, for the accounts where a position can stand without a stop until the sweep
# comes: an order resting that may open, a position with no stop, positions or stops that changed
# since the last pass, an action just sent (risk-desk verdict, 5 Nov 2026: "30 base, 15 where it
# can be blind"). Every other account waits the longer period above.
PROTECT_HOT_EVERY_S = 15.0
# How often the gateway writes what it spent of Hyperliquid's minute to its log (gateway/weight.py).
WEIGHT_LOG_EVERY_S = 60.0
# An account is read again this long after an order that may open went: long enough for Hyperliquid to
# show the fill, short of the next sweep (ALGO, 21 Oct 2026).
AFTER_ENTRY_S = 2.0
# The gateway listens before it reads the accounts its signer's keys trade, and until it has read them an
# order is answered `429 busy` (`gateway_starting`) with this Retry-After. On the host's throttled node that
# reading took 22 to 42 seconds for demo2's 92 keys (10 Nov 2026), and the gateway answered nothing meanwhile,
# not even /v1/health.
STARTING_RETRY_AFTER_S = 5
# When the start's reading fails as a whole (the signer's list of keys, say), it is tried again after 1 s, then
# 2, 4 and so on, never more than this apart. A key whose own read fails is not this: the sweep tries it again.
TAKE_ON_RETRY_MAX_S = 60.0


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
        caps = size_caps(env.get("GATEWAY_SIZE_CAPS", ""))
        if not caps:
            # Said at the start, not as a refusal of every order: a host updated without the setting
            # (bootstrap.sh writes gateway.env only once) would otherwise look up and sign nothing.
            raise SystemExit("refusing to start: GATEWAY_SIZE_CAPS is empty, so the demo signer would sign "
                             "nothing; set it to the platform's perps and caps (docs/HOSTING.md)")
        return mode, DemoSigner(DemoSigner.load_keys(env["GATEWAY_KEYS_DIR"]), caps=caps)
    if mode == "signer":
        return mode, SignerClient(env["SIGNER_URL"], SignerClient.load_tokens(env["SIGNER_TOKENS_FILE"]))
    raise SystemExit("GATEWAY_SIGNER must be demo or signer")


class Gateway:
    def __init__(self, reader: ChainReader, signer: DemoSigner | SignerClient, submit=hl.submit, clock=time.time,
                 venue=None, leverage_caps: dict[int, Decimal] | None = None, after_entry_s: float | None = None,
                 hold_over_weekend: frozenset[str] = frozenset(), schedule=None):
        self.reader = reader
        self.signer = signer
        self.submit = submit
        self.clock = clock
        self.nonces = NonceBook()
        self._own_nonce = 0
        self._own_nonce_lock = threading.Lock()
        self.protector = Protector(reader, venue if venue is not None else hl.Venue(), self._act, clock,
                                   asset_caps=leverage_caps, hold_over_weekend=hold_over_weekend)
        # How long after an order that may open the account is read again (`Protector.after_entry`);
        # None reads it only at the next sweep, as the tests build the gateway.
        self.after_entry_s = after_entry_s
        self.schedule = schedule if schedule is not None else later

    def handle_order(self, body: Any) -> tuple[int, dict]:
        # Everything that reaches the two handlers below was raised BEFORE the trader's action was
        # submitted: what follows the submit catches its own failures (`_handle_held`). So each of
        # these answers carries `sent: false`, and none of them is a 5xx -- see `not_sent`.
        try:
            return self._handle(body)
        except GatewayError as e:
            payload = {"status": "refused_by_gateway", "code": e.code, "detail": e.detail}
            if e.code == "replayed":
                # This copy was not sent, but its nonce was taken by one that may have been: a client
                # that gave the attempt back here would count an order that could be on the book.
                return e.status, payload
            return not_sent(e.status, payload)
        except Throttled:
            # The node is rate limiting us, which is not the same as being broken: waiting
            # helps, and the trader can be told so. 429 and not a 5xx on purpose -- the CDN in
            # front of this replaces the body of a 5xx with its own page (measured 24 Sep 2026,
            # a trader's order came back as Cloudflare's HTML), and then nothing this gateway
            # said reaches anyone.
            log_line(event="upstream_busy")
            return not_sent(429, {"status": "busy", "code": "upstream_busy",
                                  "detail": "the chain node is refusing reads right now; try again in a moment"})
        except Exception as e:  # a chain read or the Signer failed; nothing was submitted
            log_line(event="upstream_failed", error=type(e).__name__)
            return not_sent(502, {"status": "gateway_error", "code": "upstream_failed"})

    def _handle(self, body: Any) -> tuple[int, dict]:
        req = Request.from_json(body)
        cleared = check(req, self.reader, int(self.clock() * 1000), self.nonces)
        # One request at a time per account, from the stop and take placed for it to Hyperliquid's
        # answer: a sweep, or a second order for the same account, in between would read a book
        # without this order and protect the account for less than it is about to hold.
        with self.protector.holding(req.account):
            try:
                return self._handle_held(req, cleared)
            finally:
                # Whatever came of it -- sent, refused, or broken off between the read of the account and
                # the answer -- the account's kept orders list goes: the next read takes them afresh.
                self.protector.forget_orders(req.account)

    def _handle_held(self, req: Request, cleared) -> tuple[int, dict]:
        # check() claimed the nonce, so a copy of this request arriving meanwhile is refused
        # without a signer call. Until a signature for the right key exists, nothing can
        # reach Hyperliquid, and any failure gives the nonce back for a retry.
        protection = None
        if self.protector.agent_gone(cleared.key):
            self.nonces.release(cleared.trader, req.nonce)
            raise GatewayError(502, "agent_missing",
                               "Hyperliquid does not know the account's trading key as its agent; "
                               "nothing is signed for it until the next attempt")
        try:
            if req.kind in ("stop", "take"):
                action = self.protector.move(req.account, cleared.key, "sl" if req.kind == "stop" else "tp",
                                             req.asset, Decimal(req.message["triggerPx"]))
                signature, status, payload = self._sign(req, cleared, action, "protect")
            else:
                if req.kind == "cancel":
                    self.protector.refuse_protective_cancel(req.account, req.message["oid"])
                # Before anything is signed: an order after which the account could break the
                # pool's leverage rule is refused here, from the same read of the account that the
                # stop and the take are then placed from.
                read = None
                if req.opens():
                    read = self.protector.before_signing(
                        req.account, cleared.key, req.asset, req.message["isBuy"], Decimal(req.message["size"]))
                action = req.action()
                signature, status, payload = self._sign(req, cleared, action, req.kind)
                # The stop and the take go first: an order that may open a position is never
                # submitted without them on the book.
                if signature is not None and req.opens():
                    protection = self.protector.before_opening(
                        req.account, cleared.key, req.asset, req.message["isBuy"], Decimal(req.message["size"]),
                        avoid_nonce=req.nonce, read=read)
        except BaseException:
            self.nonces.release(cleared.trader, req.nonce)
            raise
        if signature is None:
            self.nonces.release(cleared.trader, req.nonce)
            return not_sent(status, payload)

        # From here the nonce stays spent: the venue may have the order even if the call fails.
        base = payload if protection is None else {**payload, "protection": protection}
        # Before it goes, not after: whatever comes of the call, the account's kept orders list is gone
        # and the sweep comes back to it at the shorter period.
        self.protector.note_sent(req.account)  # the next read takes its orders afresh
        try:
            venue = self.submit(action, req.nonce, signature)
        except Exception as e:
            # Past the submit: the action may be on Hyperliquid, so nothing in this branch may turn
            # the answer into "not sent" (audit, cp#156). The guard is the code's, not the line's.
            try:
                self._trader_asked(req, action)  # it may have reached Hyperliquid all the same
                log_line(event="venue_unreachable", error=type(e).__name__)
            except Exception as fault:
                log_line(event="after_submit_failed", error=type(fault).__name__)
            return 502, {**base, "status": "venue_unreachable",
                         "detail": "the order may or may not have reached Hyperliquid; check the account"}
        try:
            refusal, confirmed = hl.venue_outcome(venue)
            if agent_unknown(refusal, cleared.key):
                self.protector.note_agent_gone(cleared.key)
            if refusal is not None:
                return 422, {**base, "status": "refused_by_venue", "reason": refusal, "venue": venue}
            self._trader_asked(req, action)  # confirmed, or an answer that confirms nothing: either way it may stand
        except Exception as e:
            # Past the submit: whatever failed here, the action went to Hyperliquid, so this must
            # never fall through to `handle_order`, whose answers say nothing was sent.
            log_line(event="after_submit_failed", error=type(e).__name__)
            return 502, {**base, "status": "venue_unconfirmed", "venue": venue,
                         "detail": "the order went to Hyperliquid and its answer could not be read; check the account"}
        try:
            self._read_after_entry(req, cleared.key)
        except Exception as e:  # past the submit: never an answer that says nothing was sent (CodeRabbit, cp#165)
            log_line(event="after_entry_schedule_failed", error=type(e).__name__)
        if not confirmed:
            return 502, {**base, "status": "venue_unconfirmed", "venue": venue,
                         "detail": "Hyperliquid's answer confirms nothing; check the account"}
        return 200, {**base, "status": "submitted", "venue": venue}

    def _read_after_entry(self, req: Request, key: str) -> None:
        """An order that may open went to Hyperliquid: its account is read again a moment later, off
        the trader's answer, and a position without a stop gets one then (part 5, ALGO 21 Oct 2026)."""
        if self.after_entry_s is not None and req.opens():
            self.schedule(self.after_entry_s, lambda: self.protector.after_entry(req.account, key))

    def _trader_asked(self, req: Request, action: dict) -> None:
        """A take the trader asked to move is theirs from here on, unless Hyperliquid refused it.
        Which direction it closes is in the order itself: a take that buys closes a short."""
        if req.kind == "take":
            wire = action["modifies"][0]["order"] if action["type"] == "batchModify" else action["orders"][0]
            self.protector.trader_asked(req.account, req.asset, SHORT if wire["b"] else LONG,
                                        Decimal(req.message["triggerPx"]))

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
        way a trader's is checked, and submits it. A cancel of its own stop or take is signed as a
        cancel: the signer's policy for the gateway's protection knows orders and modifies alone.
        Returns (why it failed, whether Hyperliquid confirmed it, Hyperliquid's answer)."""
        nonce = self._next_nonce(avoid_nonce)
        # A cancel is signed as a cancel, a market close of a position (grouping "na") as a close, and
        # the stop and the take as protection: the signer's policy knows each by its kind.
        kind = "cancel" if action.get("type") == "cancel" else "close" if action.get("grouping") == "na" else "protect"
        try:
            signed = self.signer.sign(key, kind, action, nonce)
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


def later(delay: float, fn) -> None:
    """Runs `fn` once, `delay` seconds from now, on a thread of its own that does not hold the process."""
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


# The HTTP status of an answer that says the trader's action was NOT submitted, when it would
# otherwise be a 5xx. The CDN in front of the gateway replaces the body of a 5xx with its own HTML
# page (measured 24 Sep 2026, and again on 19 Oct 2026: two orders came back as Cloudflare's page),
# and a client that cannot read the answer has to assume the order may have gone -- so it spends
# one of the trader's few daily orders on a failure of ours. 424, "failed dependency", is what
# these are: something the gateway depends on failed, and the request was not carried out.
NOT_SENT_STATUS = 424


def not_sent(status: int, payload: dict) -> tuple[int, dict]:
    """An answer for which the trader's action never left the gateway: `sent: false`, and never a 5xx.
    A client may give the attempt back on `sent: false` and on nothing else (agents/desk.py)."""
    return (NOT_SENT_STATUS if status >= 500 else status), {**payload, "sent": False}


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
                 health: dict | None = None, faucet: Faucet | None = None, contacts: Contacts | None = None,
                 live: Any = None, starting: "Starting | None" = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "colosseum-pools-gateway"
        timeout = request_timeout  # applies to every read and write on the connection

        def _cors(self) -> None:
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Vary", "Origin")

        def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
            data = json.dumps(payload, default=str).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
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
                now = {}
                if live is not None:
                    try:
                        now = live()
                    except Exception as exc:  # a count that fails must not take the health answer with it
                        log_line(event="health_live_failed", error=type(exc).__name__)
                ready = starting.summary() if starting is not None else {}
                self._send(200, {"ok": True, "chain_id": CHAIN_ID, **(health or {}), **ready, **now})
                return
            url = urlsplit(self.path)
            if url.path == "/v1/contacts":
                if contacts is None:
                    self._no_contacts()
                    return
                pool = parse_qs(url.query).get("pool", [""])[0]
                self._send(*contacts.read(pool))
                return
            self._send(404, {"status": "not_found"})

        def _no_contacts(self) -> None:
            self._send(404, {"status": "not_found", "code": "no_contacts_here",
                             "detail": "this gateway keeps no contacts"})

        def _body(self) -> tuple[bool, Any]:
            """The request's JSON, or (False, None) once the refusal of it has been sent."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"status": "refused_by_gateway", "code": "bad_size"})
                return False, None
            try:
                return True, json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, {"status": "refused_by_gateway", "code": "bad_json"})
                return False, None

        def do_POST(self):  # noqa: N802
            if self.path == "/v1/faucet":
                self._faucet()
                return
            if self.path == "/v1/contacts":
                if contacts is None:
                    self._no_contacts()
                    return
                ok, body = self._body()
                if not ok:
                    return
                status, payload = contacts.write(body)
                log_line(http=status, contacts=True, **{k: payload.get(k) for k in ("status", "code", "pool")})
                self._send(status, payload)
                return
            if self.path != "/v1/order":
                self._send(404, {"status": "not_found"})
                return
            ok, body = self._body()
            if not ok:
                return
            if starting is not None and not starting.ready():
                # Read and then refused, so that nothing of it is left on the connection. 429 and not a 5xx,
                # like nginx's `gateway_restarting` while the gateway does not listen: the CDN replaces a 5xx's body.
                status, payload = not_sent(429, {"status": "busy", "code": "gateway_starting",
                                                 "detail": starting.detail()})
                log_line(http=status, code="gateway_starting")
                self._send(status, payload, {"Retry-After": str(STARTING_RETRY_AFTER_S)})
                return
            status, payload = gw.handle_order(body)
            log_line(http=status, **{k: payload.get(k) for k in ("status", "code", "account", "trader", "key")})
            self._send(status, payload)

        def _faucet(self) -> None:
            if faucet is None:
                self._send(404, {"status": "not_found", "code": "no_faucet", "detail": "this gateway has no faucet"})
                return
            ok, body = self._body()
            if not ok:
                return
            # nginx puts the client's address here, as Cloudflare saw it (ops/host/nginx-pools-api.conf.in);
            # the gateway listens on 127.0.0.1, so nothing else reaches it to say otherwise.
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            status, payload = faucet.grant(body, ip)
            log_line(http=status, faucet=True, **{k: payload.get(k) for k in ("status", "code", "address")})
            self._send(status, payload)

        def log_message(self, fmt, *args):  # the JSON line above is the log
            return

    return Handler


# The release the host runs from writes the commit it is into this file (ops/host/install.sh); the
# gateway names it in `/v1/health`, so that anyone can tell which code answers.
REVISION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "REVISION")


def revision(path: str = REVISION_FILE) -> str | None:
    """The commit this release is, from its REVISION file: a full commit hash, or None."""
    try:
        with open(path, encoding="ascii") as f:
            sha = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def live_summary(protector: Protector) -> dict:
    """What `/v1/health` adds at each request: Hyperliquid's weight this process sent in the last minute,
    by request type (gateway/weight.py), the accounts the sweep watches, holding a position, and hot, and the
    keys whose account the node has not let it read yet (`keys_unread`, tried again by every sweep).
    The ceiling of accounts a host takes is then read off the host, not worked out from the docs."""
    return {"weight_per_min": METER.per_minute(), "accounts_watched": len(protector.watching()),
            "accounts_with_position": protector.accounts_with_position(), "accounts_hot": len(protector.hot()),
            "keys_unread": protector.unread_keys()}


def log_weight(protector: Protector, every: float = WEIGHT_LOG_EVERY_S, stop: threading.Event | None = None) -> None:
    """The same, written to the log once a minute: `journalctl -u colosseum-gateway | grep weight_per_min`."""
    stop = stop or threading.Event()
    while not stop.wait(every):
        try:
            log_line(event="weight_per_min", **live_summary(protector))
        except Exception as exc:
            log_line(event="weight_log_failed", error=type(exc).__name__)


class Starting:
    """Whether the gateway has read, since it started, the accounts its signer's keys trade.

    It listens first and reads them after (`take_on_at_start`): on the host's throttled node the reading took
    22 to 42 seconds (10 Nov 2026), and while it came before the server, nothing answered at all -- a client could
    not tell a gateway starting from one that was gone. Until the reading is done, /v1/health says so with
    `ready: false` and how long the start has taken, and an order is refused with `gateway_starting` and
    `sent: false`. Nothing else waits for it: the faucet and the contacts have nothing to do with the keys.
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self.since = clock()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self.failed_attempts = 0
        self.last_error: str | None = None

    def ready(self) -> bool:
        return self._done.is_set()

    def failed(self, exc: Exception) -> None:
        with self._lock:
            self.failed_attempts += 1
            self.last_error = str(exc)[:200]

    def done(self) -> None:
        self._done.set()

    def detail(self) -> str:
        return (f"the gateway started {self.clock() - self.since:.0f} s ago and is still reading the accounts its "
                f"keys trade; nothing was sent, send it again in {STARTING_RETRY_AFTER_S} seconds")

    def summary(self) -> dict:
        """What /v1/health adds: `ready`, and while it is false, what the start is doing and for how long."""
        if self.ready():
            return {"ready": True}
        with self._lock:
            out = {"ready": False, "starting_for_s": round(self.clock() - self.since, 1),
                   "starting": "reading the accounts the signer's keys trade"}
            if self.failed_attempts:
                out["start_failed_attempts"] = self.failed_attempts
                out["start_last_error"] = self.last_error
        return out


def take_on_at_start(gateway: Gateway, reader: Any, signer: Any, starting: Starting, every: float, hot_every: float,
                     wait=time.sleep, retry_max_s: float = TAKE_ON_RETRY_MAX_S) -> None:
    """After the server listens: the accounts the signer's keys trade, then the names of their markets, then the
    sweep; the gateway is `ready` once the sweep has started.

    A key whose read fails is kept by `take_on` and tried again by every sweep until the node answers. A reading
    that fails as a whole is tried again here, 1 s after, then 2, 4 and so on up to `retry_max_s` apart, for as long
    as it takes: a gateway that gave up would stay `ready: false` for good, and one that went on without its keys'
    accounts would leave a position open over a restart unswept until its trader's next order."""
    # Before the sweep starts: the accounts this signer's keys trade right now, so one with a position
    # open is swept from the first pass and not from its trader's next order.
    addresses = getattr(signer, "addresses", None)
    delay = 1.0
    while True:
        try:
            taken = gateway.protector.take_on(addresses()) if addresses else []
            break
        except Exception as exc:
            starting.failed(exc)
            log_line(event="protect_take_on_retry", error=str(exc)[:200], wait_s=delay)
            wait(delay)
            delay = min(delay * 2, retry_max_s)
    log_line(event="protect_took_on", accounts=taken, keys_listed=addresses is not None)
    # Every asset those accounts may trade, its name on HyperEVM against Hyperliquid's, before the
    # first order: a mismatch is refused at the order anyway, and said here first.
    try:
        names = gateway.protector.check_names(set().union(*(reader.allowed_assets(a) for a in taken)) if taken else set())
        log_line(event="market_names", checked={str(a): v for a, v in names.items()})
    except Exception as exc:  # said, and the order checks the name itself; it must not hold the start
        log_line(event="market_names_failed", error=str(exc)[:200])
    threading.Thread(target=gateway.protector.run, args=(every, None, hot_every), name="protect-sweep", daemon=True).start()
    starting.done()
    log_line(event="ready", after_s=round(starting.clock() - starting.since, 1))


def health_summary(mode: str, signer: Any, protect_every: float, commit: str | None = None,
                   protect_hot_every: float | None = None) -> dict:
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
    if protect_hot_every is not None:
        out["protect_hot_every"] = protect_hot_every
    if commit is not None:
        out["commit"] = commit
    return out


def leverage_caps_from_env(env, signer) -> dict[int, Decimal]:
    """GATEWAY_LEVERAGE_CAPS, refused at the start when it is empty or, with the demo signer, leaves
    out a perp GATEWAY_SIZE_CAPS lists: either way an order would be refused at the gateway for a
    setting the host lacks, and a start that says so is found at once."""
    caps = leverage_caps(env.get("GATEWAY_LEVERAGE_CAPS", ""))
    if not caps:
        raise SystemExit("refusing to start: GATEWAY_LEVERAGE_CAPS is empty, so no perp has a leverage cap; "
                         "set it to the platform's perps and caps (docs/HOSTING.md)")
    missing = sorted(set(getattr(signer, "caps", {})) - set(caps))
    if missing:
        raise SystemExit(f"refusing to start: GATEWAY_LEVERAGE_CAPS has no cap for {missing}, which GATEWAY_SIZE_CAPS lists")
    return caps


def refuse_testnet_parts(env, net: network.Network = NETWORK) -> None:
    """Stops the start where a part made for testnet alone is asked for on another network: the demo
    signer signs with testnet keys made for the demo, and the faucet hands out the demo's dUSDC by a
    testnet deployment's record (gateway/faucet.py). On testnet it does nothing."""
    if net.name == "testnet":
        return
    if env.get("GATEWAY_SIGNER") == "demo":
        raise SystemExit(f"refusing to start: the demo signer is for testnet only, and {network.ENV} is {net.name}")
    if env.get("GATEWAY_FAUCET_KEY_FILE"):
        raise SystemExit(f"refusing to start: the faucet is for testnet only, and {network.ENV} is {net.name}; "
                         "unset GATEWAY_FAUCET_KEY_FILE")


def main() -> int:
    refuse_testnet_parts(os.environ)
    rpc = os.environ.get("GATEWAY_RPC_URL", DEFAULT_RPC)
    reader = JsonRpcReader(rpc, os.environ["GATEWAY_FACTORY"], os.environ["GATEWAY_REGISTRY"])
    reader.check_chain()
    mode, signer = signer_from_env(os.environ)
    caps = leverage_caps_from_env(os.environ, signer)
    log_line(event="leverage_caps", caps={str(a): str(c) for a, c in sorted(caps.items())})
    host, _, port = os.environ.get("GATEWAY_BIND", "127.0.0.1:8787").partition(":")
    allow_origin = os.environ.get("GATEWAY_ALLOW_ORIGIN", "")  # the app's origin, if it calls from a browser
    every = float(os.environ.get("GATEWAY_PROTECT_EVERY", PROTECT_EVERY_S))
    hot_every = float(os.environ.get("GATEWAY_PROTECT_HOT_EVERY", PROTECT_HOT_EVERY_S))
    if not every > 0 or not hot_every > 0:
        raise SystemExit(f"refusing to start: GATEWAY_PROTECT_EVERY ({every}) and GATEWAY_PROTECT_HOT_EVERY "
                         f"({hot_every}) must be more than 0 seconds")
    health = health_summary(mode, signer, every, revision(), protect_hot_every=hot_every)
    # A deployment with a faucet, and GATEWAY_FAUCET_KEY_FILE set: checked against its record and the
    # token before anything listens (gateway/faucet.py).
    faucet = faucet_from_env(os.environ, reader.rpc, pathlib.Path(__file__).resolve().parents[1], log=log_line)
    if faucet is not None:
        health["faucet"] = faucet.summary()
    log_line(event="faucet", on=faucet is not None, **(faucet.summary() if faucet else {}))
    # Pool owners' contacts, where the service has a directory for them (gateway/contacts.py).
    contacts = contacts_from_env(os.environ, reader, log=log_line)
    health["contacts"] = contacts is not None
    log_line(event="contacts", on=contacts is not None)
    # The perp dexes the gateway reads besides Hyperliquid's own (HIP-3), comma-separated: none
    # unless set, and a perp of one is still traded only if the platform's list names it.
    dexes = tuple(d.strip() for d in os.environ.get("GATEWAY_PERP_DEXES", "").split(",") if d.strip())
    # The pools whose accounts keep HIP-3 positions over the weekend, comma-separated: none unless set,
    # so every pool is flat over it, the default (CTO, 21 Oct 2026).
    hold = frozenset(p.strip().lower() for p in os.environ.get("GATEWAY_HOLD_OVER_WEEKEND", "").split(",") if p.strip())
    log_line(event="hold_over_weekend", pools=sorted(hold))
    gateway = Gateway(reader, signer, venue=hl.Venue(dexes=(hl.MAIN_DEX, *dexes)), leverage_caps=caps,
                      after_entry_s=AFTER_ENTRY_S, hold_over_weekend=hold)
    # The server is bound first, and the accounts the keys trade are read after it, in their own thread
    # (`take_on_at_start`): /v1/health answers from the first second, with `ready: false` until then.
    starting = Starting()
    server = BoundedServer((host, int(port)), make_handler(gateway, allow_origin, health=health, faucet=faucet,
                                                                contacts=contacts,
                                                                live=lambda: live_summary(gateway.protector),
                                                                starting=starting))
    threading.Thread(target=take_on_at_start, args=(gateway, reader, signer, starting, every, hot_every),
                     name="take-on-at-start", daemon=True).start()
    threading.Thread(target=log_weight, args=(gateway.protector,), name="weight-log", daemon=True).start()
    print(f"gateway listening on {host}:{port}, signer: {mode}, stop and take swept every {every:g} s, "
          f"{hot_every:g} s where a position can stand without a stop, once it has read its keys' accounts", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
