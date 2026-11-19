"""The tests of ops/: none of them reads a real key or reaches a live node.

This file runs before any test module of the package, whether unittest discovers them (CI) or a run names one
(scripts/mutations.py). It points COLOSSEUM_KEY_DIR at an empty directory of its own, and it installs an audit hook
(PEP 578, the standard library's) that fails the test, saying why, when anything in it opens or lists a path under
~/secure, or connects to or looks up any host but this machine: testnet's and mainnet's RPC and Hyperliquid's API
alike. A local node on 127.0.0.1 and stand-ins pass. The failure is not an Exception, so no `except Exception` in
the code under test swallows it.

Why: a test that expects a refusal goes the real way under a mutant that takes the refusal away. On 19 November 2026
one read testnet's deployer key and switched its big blocks on and off on testnet; with a mainnet key directory on
the same machine it would have sent on mainnet.
"""

import os
import socket
import sys
import tempfile


class GuardTripped(BaseException):
    """A test reached a real key or a live node: the test fails, and nothing is read or sent."""


SECURE = tuple({os.path.abspath(os.path.expanduser("~/secure")), os.path.realpath(os.path.expanduser("~/secure"))})
# Each test process gets an empty key directory of its own: a key read by name finds nothing.
KEY_DIR = tempfile.mkdtemp(prefix="ops-tests-no-keys-")
os.environ["COLOSSEUM_KEY_DIR"] = KEY_DIR


def under_secure(path) -> str | None:
    """`path` (as `open` or `os.listdir` takes it) if it is ~/secure or under it, else None."""
    if path is None or isinstance(path, int):
        return None
    try:
        text = os.path.abspath(os.fsdecode(os.fspath(path)))
    except TypeError:
        return None
    for p in {text, os.path.realpath(text)} if "secure" in text else {text}:
        if any(p == s or p.startswith(s + os.sep) for s in SECURE):
            return p
    return None


def local(host) -> bool:
    """Whether `host` is this machine: a loopback address, localhost, or none (a lookup of a port alone)."""
    if host in (None, b"", ""):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).lower().strip("[]")
    return host in ("localhost", "::1") or host.startswith("127.")


def _hook(event: str, args: tuple) -> None:
    if event in ("open", "os.listdir", "os.scandir") and args:
        path = under_secure(args[0])
        if path:
            raise GuardTripped(f"ops/tests guard: a test opened {path}; the tests never read a real key: stand in for "
                               "c.account and every send path (ops/tests/__init__.py)")
    elif event == "socket.getaddrinfo" and not local(args[0]):
        raise GuardTripped(f"ops/tests guard: a test looked up {args[0]!r}; the tests reach only 127.0.0.1 and "
                           "stand-ins, never a live node or Hyperliquid's API (ops/tests/__init__.py)")
    elif event == "socket.connect":
        sock, address = args
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not local(address[0]):
            raise GuardTripped(f"ops/tests guard: a test connected to {address[0]}:{address[1]}; the tests reach only "
                               "127.0.0.1 and stand-ins, never a live node or Hyperliquid's API (ops/tests/__init__.py)")


# Once a process: a second import of this file (by another name) adds no second hook.
if not getattr(sys, "_ops_tests_guard", False):
    sys.addaudithook(_hook)
    sys._ops_tests_guard = True
