"""Each asset's own leverage cap, `Rules.assetLeverageX100` (the CTO, 21 October 2026).

The cap a pool's rules give an asset unless its plan or its command line names another, times 100.
Never over the account's own cap (`maxLeverageX100`): the contracts refuse that, so here the default
is the lower of the two. A builder dex's market is found by its coin ("xyz:XYZ100" is XYZ100).
"""

from __future__ import annotations

ASSET_LEVERAGE_X100 = {"BTC": 1000, "ETH": 1000, "SOL": 500, "HYPE": 500,
                       "SP500": 1000, "XYZ100": 800, "CL": 300, "BRENTOIL": 200}


def coin(name: str) -> str:
    return name.split(":")[-1]


def asset_caps(names, total: int, chosen: dict | None = None) -> list[int]:
    """Rules.assetLeverageX100 for the assets `names`, in their order: what `chosen` names for one
    (by its full name or its coin), else the default above at most `total`, else `total`."""
    chosen = chosen or {}
    out = []
    for name in names:
        default = min(ASSET_LEVERAGE_X100.get(coin(name), total), total)
        out.append(int(chosen.get(name, chosen.get(coin(name), default))))
    return out


def bad_caps(names, caps, total: int) -> list[str]:
    """The assets whose cap the contracts would refuse: under 1x or over the account's cap."""
    return [name for name, cap in zip(names, caps) if not 100 <= cap <= total]


def caps_text(names, caps) -> str:
    """"BTC 5x, ETH 5x": each asset's cap as the terms say it."""
    return ", ".join(f"{name} {cap / 100:g}x" for name, cap in zip(names, caps))
