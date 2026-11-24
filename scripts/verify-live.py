#!/usr/bin/env python3
"""The site as it is served, checked against the pack it was uploaded from.

    python3 scripts/verify-live.py [--network testnet|mainnet] <pack>.sha256 https://pools.usenami.io
    python3 scripts/verify-live.py [--network testnet|mainnet] <pack>.sha256 <a folder standing in for the site>

The list is the one scripts/pack-site.sh writes beside the pack: a sha256 for every file of the pack, build.json
included. Every file in it is read from the site and hashed. Exit 0 only if every one matches; 1 if any differs or
cannot be read, each named on a line of its own; 2 if the arguments or the list cannot be used.

--network says which network's site the address is, testnet unless named. The site's own build.json names the
network its pack was made for (scripts/pack-site.sh), and a pack made for the other network is refused with exit 1
before a file is compared: every file of a mainnet pack uploaded over the testnet site would match its own list,
and the check would have confirmed the wrong site. A build.json that names no network is a pack from before the
packer named one, which was only ever testnet's.

index.html is read from "/", the address a visitor opens. Cloudflare inserts two tags into it before </body>, in
either order: its challenge script, and its Web Analytics beacon followed by a newline. Each is cut on its own, so
the rest has to match the pack byte for byte. Each is cut only in the exact form Cloudflare serves it, where the
ids and tokens are the only parts that vary, and each of those within its own characters and length. A tag that is
anything more -- another body, another attribute -- stays in, and index.html is a mismatch. So is the day
Cloudflare changes its snippet, until the pattern here is brought up to it.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# The challenge script as Cloudflare serves it before </body> (read from the site on 8 November 2026), word for
# word. @RAY@ is the request's ray id, in both places the same; @TIME@ is the time it was served, in base64.
CHALLENGE_TEXT = (
    "<script>(function(){function c(){var b=a.contentDocument||(a.contentWindow&&a.contentWindow.document);"
    "if(b){var d=b.createElement('script');d.innerHTML=\"window.__CF$cv$params={r:'@RAY@',t:'@TIME@'};"
    "var l=window.parent.location,o;"
    "try{o=new URL(l.origin).origin}catch(e){try{o=new URL(new URL(l.href).origin).origin}catch(e){o=''}}"
    "var u='/cdn-cgi/challenge-platform/scripts/jsd/main.js?ray=@RAY@',a=document.createElement('script');"
    "a.src=o?new URL(u,o).href:u;document.getElementsByTagName('head')[0].appendChild(a);\";"
    "b.getElementsByTagName('head')[0].appendChild(d)}}if(document.body){var a=document.createElement('iframe');"
    "a.height=1;a.width=1;a.style.position='absolute';a.style.top=0;a.style.left=0;a.style.border='none';"
    "a.style.visibility='hidden';document.body.appendChild(a);if('loading'!==document.readyState)c();"
    "else if(window.addEventListener)document.addEventListener('DOMContentLoaded',c);"
    "else{var e=document.onreadystatechange||function(){};document.onreadystatechange=function(b){e(b);"
    "'loading'!==document.readyState&&(document.onreadystatechange=e,c())}}}})();</script>"
)
CHALLENGE = re.compile(
    re.escape(CHALLENGE_TEXT)
    .replace("@RAY@", "(?P<ray>[0-9a-f]{16})", 1)
    .replace("@RAY@", "(?P=ray)", 1)
    .replace("@TIME@", "[A-Za-z0-9+/]{2,64}={0,2}", 1)
    .encode())
# The Web Analytics beacon: these attributes in this order and no others. `defer` is how Cloudflare loaded it before
# `type="module"`. The beacon's own script comes from Cloudflare's host alone, and its settings are JSON with
# nothing in it that could close the attribute or open a tag.
BEACON = re.compile(
    rb'<script (?:type="module"|defer) src="https://static\.cloudflareinsights\.com/beacon\.min\.js/v[0-9a-f]{1,64}"'
    rb' integrity="sha512-[A-Za-z0-9+/]{86}=="'
    rb" data-cf-beacon='\{[^<>'\\]{1,512}\}'"
    rb' crossorigin="anonymous"></script>\n?')
LINE = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
NETWORKS = ("testnet", "mainnet")  # the ones scripts/pack-site.sh packs for
BUILD = "build.json"


def network_of(get):
    """The network the site's pack was made for, as its build.json names it; testnet for one that names none."""
    body, _ = get(BUILD)
    build = json.loads(body)
    if not isinstance(build, dict):
        raise ValueError(f"{BUILD} is not an object")
    return build.get("network", "testnet")


def entries(path):
    """The list's (sha256, file) pairs, with shasum's "./" taken off; a line of anything else is refused."""
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\r\n")
            if not line:
                continue
            m = LINE.match(line)
            rel = m.group(2) if m else ""
            if rel.startswith("./"):
                rel = rel[2:]
            if not m or rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(f"{path}:{n} is not a sha256 line of a file in the pack: {line[:80]!r}")
            out.append((m.group(1), rel))
    if not out:
        raise ValueError(f"{path} names no file: an empty list would pass any site")
    return out


def reader(base):
    """How the site is read: over HTTP(S) with a cache-buster, or from a folder on disk."""
    if base.startswith(("https://", "http://")):
        def get(rel):
            path = "" if rel == "index.html" else urllib.parse.quote(rel)
            req = urllib.request.Request(f"{base}/{path}?v={time.time_ns() // 1_000_000}",
                                         headers={"User-Agent": "pack-verify", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read(), f"{r.status} {r.headers.get('Content-Type', '')}"
        return get, "index.html from /"
    if not os.path.isdir(base):
        raise ValueError(f"{base} is neither an http(s) address nor a folder")

    def read(rel):
        with open(os.path.join(base, rel), "rb") as f:
            return f.read(), "file"
    return read, "index.html"


def main(argv):
    args, want = argv[1:], "testnet"
    if args[:1] == ["--network"]:
        want, args = (args[1] if len(args) > 1 else ""), args[2:]
    if len(args) != 2 or want not in NETWORKS:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    sums, base = args[0], args[1].rstrip("/")
    try:
        files = entries(sums)
        get, index_from = reader(base)
    except (OSError, ValueError) as e:
        print(f"verify-live: {e}", file=sys.stderr)
        return 2
    print("read at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    # Which network's pack the site serves, before any file is compared: a pack of the other network matches its own
    # list file for file, and that match must not be read as this site being right.
    try:
        got = network_of(get)
    except Exception as e:  # no build.json, or one that cannot be read: the site does not say what it is
        print(f"live {base}: its {BUILD} does not say which network its pack is for ({e}); nothing was compared")
        return 1
    if got != want:
        print(f"live {base}: serves a pack made for {got}, and this is the check of the {want} site; nothing was "
              f"compared. If this address is the {got} site, say so: --network {got}")
        return 1
    match = differ = 0
    notes = []
    for want, rel in files:
        try:
            body, how = get(rel)
        except Exception as e:  # a refused or failed read is a file the site does not serve as packed
            differ += 1
            print("ERR ", rel, e)
            continue
        if rel == "index.html":
            n_challenge, n_beacon = len(CHALLENGE.findall(body)), len(BEACON.findall(body))
            body = BEACON.sub(b"", CHALLENGE.sub(b"", body))
            notes.append(f"{index_from}: cut Cloudflare's challenge script x{n_challenge} and Web Analytics beacon "
                         f"x{n_beacon} before comparing")
        if hashlib.sha256(body).hexdigest() == want:
            match += 1
        else:
            differ += 1
            print("DIFF", rel, how, len(body))
    print(f"live {base}: match={match} differ={differ} of {match + differ}")
    for note in notes:
        print(note)
    return 0 if differ == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
