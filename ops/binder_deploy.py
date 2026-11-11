"""The key binder's part of a deployment (src/KeyBinder.sol, docs/KEY-BINDING.md): its settings as the deploy
scripts take them, the steps that configure it, the handover of the binder and the factory to their owner, and
what the record says of it.

    --safe             the multisig (Safe) that will own the binder and operate the factory. Both are handed
                       over in two steps: the deployer names it, and the owner accepts from the Safe itself
                       (acceptOwnership on the binder, acceptOperator on the factory). Until it does the
                       deployer holds both.
    --publisher        the address whose signature binds a key, and the hashes the event carries:
    --pcr-hash         of the enclave's measurements, and
    --attestation-hash of its attestation document. Neither may be zero (the binder refuses them). On testnet
                       before the enclave the publisher is a key in a file; its hashes then name that, see
                       TEST_PUBLISHER_NOTE.
    --guardian         the key that can only stop: pause, lower the daily limit, revoke a bound key.
    --max-binds-per-day the fuse: bindings and commitments per UTC day. Testnet 50 unless told; mainnet has
                       no default, the owner sets it.
    --renew-delay      seconds between a renewal's commitment and its key being shown; 24 hours on both
                       networks unless told (the CTO, 11 November 2026), at most the binder's 7 days.

A run without --publisher deploys a binder no request can be answered on until the owner appoints one; the
dry run says so. Nothing here holds a key or sends anything: the scripts do, with their own deployer.
"""

from __future__ import annotations

import dataclasses

from eth_utils import keccak, to_checksum_address

MAX_RENEW_DELAY = 7 * 86_400  # KeyBinder.MAX_RENEW_DELAY
RENEW_DELAY = 86_400
TESTNET_MAX_BINDS = 50
# What a testnet publisher that is a key in a file puts in place of the enclave's hashes: the binder takes no
# zero, and a reader of the event should not mistake it for a measurement.
TEST_PUBLISHER_NOTE = "colosseum-pools: a testnet publisher key in a file, no enclave"
TEST_PUBLISHER_HASH = "0x" + keccak(text=TEST_PUBLISHER_NOTE).hex()


@dataclasses.dataclass(frozen=True)
class BinderSettings:
    owner: str | None
    publisher: str | None
    pcr_hash: str | None
    attestation_hash: str | None
    guardian: str | None
    max_binds_per_day: int | None  # None only in a mainnet plan: the owner sets it
    renew_delay: int


def add_args(p) -> None:
    p.add_argument("--safe", help="the multisig that owns the key binder and operates the factory, checksummed; "
                   "named by transferOwnership and transferOperator, it accepts both itself")
    p.add_argument("--publisher", help="the address whose signature binds a key, checksummed")
    p.add_argument("--pcr-hash", help="0x and 64 hex digits: the hash of the publisher enclave's measurements")
    p.add_argument("--attestation-hash", help="0x and 64 hex digits: the hash of its attestation document")
    p.add_argument("--guardian", help="the key that can only stop bindings and revoke keys, checksummed")
    p.add_argument("--max-binds-per-day", type=int, help=f"the daily fuse; testnet {TESTNET_MAX_BINDS} unless told")
    p.add_argument("--renew-delay", type=int, default=RENEW_DELAY,
                   help=f"seconds a renewal's key waits before it is shown, at most {MAX_RENEW_DELAY}")


def checked_address(flag: str, value: str | None) -> str | None:
    """An address as given, refused unless checksummed and not zero: a typo in lower case is still an address,
    and a role handed to it would wait for good."""
    if value is None:
        return None
    try:
        good = to_checksum_address(value)
    except ValueError:
        raise SystemExit(f"{flag} is not an address: {value!r}") from None
    if value != good:
        raise SystemExit(f"{flag} has to be given in checksummed form, {good}, so that a typo cannot pass")
    if int(good, 16) == 0:
        raise SystemExit(f"{flag} cannot be the zero address")
    return good


def checked_hash(flag: str, value: str | None) -> str | None:
    if value is None:
        return None
    v = value.lower()
    if not (v.startswith("0x") and len(v) == 66 and all(ch in "0123456789abcdef" for ch in v[2:])):
        raise SystemExit(f"{flag} has to be 0x and 64 hex digits")
    if int(v, 16) == 0:
        raise SystemExit(f"{flag} cannot be zero: the key binder refuses it")
    return v


def settings(args, network: str) -> BinderSettings:
    """The binder's settings from the arguments, every check made before anything is read or sent."""
    publisher = checked_address("--publisher", args.publisher)
    pcr = checked_hash("--pcr-hash", args.pcr_hash)
    doc = checked_hash("--attestation-hash", args.attestation_hash)
    if publisher is None and (pcr or doc):
        raise SystemExit("--pcr-hash and --attestation-hash describe a publisher: give --publisher too")
    if publisher is not None and not (pcr and doc):
        raise SystemExit("--publisher needs --pcr-hash and --attestation-hash: the key binder takes neither as zero "
                         f"(on testnet before the enclave, both {TEST_PUBLISHER_HASH}, the keccak of "
                         f"{TEST_PUBLISHER_NOTE!r})")
    if network == "mainnet" and pcr == TEST_PUBLISHER_HASH:
        raise SystemExit("a publisher that is a key in a file is for testnet; on mainnet it is the enclave")
    max_binds = args.max_binds_per_day
    if max_binds is None and network != "mainnet":
        max_binds = TESTNET_MAX_BINDS
    if max_binds is not None and not 1 <= max_binds < 2**32:
        raise SystemExit("--max-binds-per-day must be at least 1: a binder at zero binds nothing")
    if not 0 <= args.renew_delay <= MAX_RENEW_DELAY:
        raise SystemExit(f"--renew-delay must be 0..{MAX_RENEW_DELAY} seconds: the key binder refuses more")
    return BinderSettings(checked_address("--safe", args.safe), publisher, pcr, doc,
                          checked_address("--guardian", args.guardian), max_binds, args.renew_delay)


def constructor(deployer: str, s: BinderSettings) -> tuple[list, list]:
    """KeyBinder(owner, maxBindsPerDay, renewDelay): the deployer owns it while it is configured."""
    if s.max_binds_per_day is None:
        raise SystemExit("--max-binds-per-day has no default on mainnet: the owner sets the fuse")
    return ["address", "uint32", "uint64"], [deployer, s.max_binds_per_day, s.renew_delay]


def binder_steps(binder: str, factory: str, s: BinderSettings) -> list[tuple]:
    """The binder's configuration, after the factory exists: its accounts are the factory's, its publisher and
    its guardian."""
    steps = [("setAccountSource", binder, "setAccountSource(address)", ["address"], [factory])]
    if s.publisher:
        steps.append(("setPublisher", binder, "setPublisher(address,bytes32,bytes32)", ["address", "bytes32", "bytes32"],
                      [s.publisher, bytes.fromhex(s.pcr_hash[2:]), bytes.fromhex(s.attestation_hash[2:])]))
    if s.guardian:
        steps.append(("setGuardian", binder, "setGuardian(address)", ["address"], [s.guardian]))
    return steps


def handover(binder: str, factory: str, s: BinderSettings) -> list[tuple]:
    """The binder and the factory's operator role to the owner, the deployment's last steps. The deployer holds
    both until the owner accepts each from the Safe."""
    if not s.owner:
        return []
    return [("transferOwnership", binder, "transferOwnership(address)", ["address"], [s.owner]),
            ("transferOperator", factory, "transferOperator(address)", ["address"], [s.owner])]


def record_of(deployer: str, s: BinderSettings) -> dict:
    """What the record says of the binder before its steps are sent; `after_step` marks each one sent."""
    return {"max_binds_per_day": s.max_binds_per_day, "renew_delay": s.renew_delay,
            "owner": {"from": deployer, "to": s.owner, "state": "named, not sent"} if s.owner else
            {"from": deployer, "to": None, "state": "the deployer"},
            "publisher": None, "guardian": None}


def after_step(record: dict, name: str, s: BinderSettings, deployer: str) -> None:
    b = record["key_binder"]
    if name == "setPublisher":
        b["publisher"] = {"address": s.publisher, "pcr_hash": s.pcr_hash, "attestation_doc_hash": s.attestation_hash,
                          "test_key": s.pcr_hash == TEST_PUBLISHER_HASH}
    elif name == "setGuardian":
        b["guardian"] = s.guardian
    elif name == "transferOwnership":
        b["owner"] = {"from": deployer, "to": s.owner, "state": "pending"}
    elif name == "transferOperator":
        record["factory_operator"] = {"from": deployer, "to": s.owner, "state": "pending"}


def plan_lines(s: BinderSettings) -> list[str]:
    """What a dry run says of the binder."""
    fuse = (f"{s.max_binds_per_day} a day" if s.max_binds_per_day is not None else
            "a daily fuse the owner sets (--max-binds-per-day; no default on mainnet)")
    out = [f"  key binder       KeyBinder(deployer, {fuse}, renewal delay {s.renew_delay} s), "
           "then setAccountSource to the factory"]
    if s.publisher:
        test = "  -- a key in a file, testnet only" if s.pcr_hash == TEST_PUBLISHER_HASH else ""
        out.append(f"  publisher        {s.publisher}, measurements {s.pcr_hash[:18]}, document {s.attestation_hash[:18]}{test}")
    else:
        out.append("  publisher        none: no request is answered until the owner appoints one (setPublisher)")
    out.append(f"  guardian         {s.guardian}" if s.guardian else
               "  guardian         none: only the owner can pause until one is set (setGuardian)")
    if s.owner:
        out.append(f"  would hand over  the key binder (transferOwnership) and the factory (transferOperator) to {s.owner}; "
                   "it accepts each from the Safe itself (acceptOwnership, acceptOperator)")
    else:
        out.append("  safe             the deployer keeps the key binder and the factory: no --safe")
    return out
