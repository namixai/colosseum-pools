"""The errors a pool of many traders and its parts can refuse a call with, by name, so that a refusal
reads as one line -- `refused: Ending()` -- and not as a traceback with a selector in it.

The list is the contracts' own declarations, in the ABI's form (an enum is a uint8), and a test
holds it against the source: a new error in any of these contracts fails that test until it is
named here. The last group is OpenZeppelin's, which the contracts raise through their libraries.
"""

from __future__ import annotations

import re

from eth_abi import decode as abi_decode
from eth_utils import keccak

# Where each group is declared, in the order the test reads them.
SOURCES = ("src/multi/MultiPool.sol", "src/multi/FreeExams.sol", "src/exam/PaperExam.sol", "src/Pool.sol",
           "src/RuledAccount.sol", "src/KeyRegistry.sol", "src/lib/CoreOps.sol", "src/multi/PoolShares.sol",
           "src/multi/ShareTicket.sol", "src/lib/CodeStore.sol")

ERRORS = [
    # MultiPool
    "BadPlan()", "NotOwner()", "NoSuchLine()", "Full()", "NoRoom()", "NotSold()", "StillRunning()", "NotPassed()",
    "NotTrader()", "TooLate()", "StillTrading(uint256)", "NotEnoughFree(uint64,uint64,uint64)", "BadSeat()",
    "TooSoon()", "AlreadyArmed()", "NoTarget()", "NotFlat()", "RuleBroken(uint8)", "TargetNotMet(int256,int256)",
    "BufferNeeded()", "Ending()", "Ended()", "NotEnded()", "Locked()", "NotInvited()", "NotNoter()", "NotStarted()",
    "Started()", "WrongTerms()", "TooManyFree()",
    # FreeExams
    "InviteSpent()", "InviteLapsed()", "NoFreeLeft()",
    # PaperExam
    "BadTerms()", "BadRules()", "UnsupportedSizeDecimals(uint32,uint8)", "BadStatus(uint8)", "AssetNotAllowed(uint32)",
    "ZeroSize()", "OrderPending()", "NoOrder()", "TooEarly(uint256)", "OrderLapsed()", "NoPrice(uint32)",
    "OverLeverage()", "OutsideCheckpointWindow()", "AlreadyCheckpointed()", "NoBreach()", "NotOver()",
    "NotEnoughDays(uint16,uint16)",
    # Pool, RuledAccount, KeyRegistry
    "NotChallenge()", "BadStage(uint8)", "NotReady()", "NotEnoughCapital(uint64,uint64)", "NotAllowed()", "TooEarly()",
    "KeyAvailable()", "WithdrawnThisBlock()", "SalesClosed()", "NotOwnerRun()", "TooManyAssets()", "TooManyCancels()",
    "NotStopped()", "NotOperator()", "NotPendingOperator()", "SourceAlreadySet()", "NotAnAccount(address)",
    "KeyNotNew(address)", "KeyExistsOnCore(address)", "ZeroAddress()", "NoFreeKey()", "AccountHasKey(address,address)",
    "NotBoundHere(address,address)", "TooManySpoiledKeys()",
    # PoolShares, ShareTicket, CodeStore
    "NotCreator()", "NotBound()", "AlreadyBound()", "NotOwnedHere()", "BadDeposit()", "TooMany()",
    "NotQuiet(uint8,address)", "NoValue()", "Decided(uint64)", "NotFree(uint256)", "NothingRequested()",
    "NothingOwed()", "NothingToSend()", "NotInWindow(uint64)", "NotLaunched()", "AlreadyLaunched()", "RaiseOver()",
    "RaiseShort(uint256,uint256)", "NotRefunding()", "CannotPay()", "NotShares()", "Initialized()", "NotStored()",
]
OPENZEPPELIN = [
    "SafeERC20FailedOperation(address)", "FailedDeployment()", "InvalidInitialization()", "NotInitializing()",
    "SafeCastOverflowedUintDowncast(uint8,uint256)", "SafeCastOverflowedIntToUint(int256)",
    "SafeCastOverflowedIntDowncast(uint8,int256)", "SafeCastOverflowedUintToInt(uint256)",
]
# Solidity's own: require with a message, and a panic (an overflow, a division by zero).
BUILTIN = ["Error(string)", "Panic(uint256)"]

BY_SELECTOR = {"0x" + keccak(text=e)[:4].hex(): e for e in ERRORS + OPENZEPPELIN + BUILTIN}
_DATA = re.compile(r"""['"]data['"]:\s*['"](0x[0-9a-fA-F]*)['"]""")
_SELECTOR = re.compile(r"0x[0-9a-fA-F]{8}")


def _types(sig: str) -> list[str]:
    inner = sig[sig.index("(") + 1:-1]
    return inner.split(",") if inner else []


def name(data: str) -> str:
    """The error in revert data: its name, with its arguments when it has any; the bare selector
    for an error none of the contracts declares."""
    sel = data[:10].lower()
    sig = BY_SELECTOR.get(sel)
    if sig is None:
        return sel
    types = _types(sig)
    if not types:
        return sig
    try:
        args = abi_decode(types, bytes.fromhex(data[10:]))
    except Exception:  # data cut short: the name is still worth printing
        return sig
    shown = ", ".join(repr(a) if isinstance(a, str) else str(a) for a in args)
    return f"{sig.split('(')[0]}({shown})"


def refusal(exc: Exception) -> str:
    """One line for a call the chain refused: the contract's error by name when the node passed
    its revert data on, else the start of the node's own message."""
    text = str(exc)
    m = _DATA.search(text)
    if m and len(m.group(1)) >= 10:
        return name(m.group(1))
    # No data field: a selector somewhere in the message, if one is ours (an address in front of it
    # starts with eight hex digits too, so every candidate is tried).
    for m in _SELECTOR.finditer(text):
        if m.group(0).lower() in BY_SELECTOR:
            return name(m.group(0))
    return " ".join(text.split())[:200]
