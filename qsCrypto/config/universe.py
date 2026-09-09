"""
universe.py
===========

Loader for the static Coinbase50 ∩ Binance USD-M perpetuals universe config
(qsCrypto/config/universe_coinbase50_binance.yaml). Per the port plan
(decision #3), the tradeable universe is a hand-curated YAML file, not a
runtime API-driven fetch — this module only reads and reshapes that file
into the structures the data/strategy/portfolio constructors expect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_UNIVERSE_PATH = Path(__file__).with_name("universe_coinbase50_binance.yaml")


@dataclass
class UniverseConfig:
    universe_name: str
    symbols: List[str]                              # Binance perp symbols, e.g. ["BTCUSDT", ...]
    generic_of: Dict[str, str]                       # binance_symbol -> generic ticker root
    corr_groups: Dict[str, str]                      # binance_symbol -> corr_group name
    max_corr_units: Dict[str, int]                   # corr_group name -> max_corr_units
    risk_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # binance_symbol -> overrides

    def corr_groups_by_group(self) -> Dict[str, set]:
        """Regroup {symbol: group} into {group: {symbols}}, the shape
        IBKRFuturesPortfolio/BinancePerpetualsPortfolio's corr_groups kwarg expects."""
        out: Dict[str, set] = {}
        for sym, group in self.corr_groups.items():
            out.setdefault(group, set()).add(sym)
        return out


def load_universe(path: Optional[Path] = None, pilot_only: bool = True) -> UniverseConfig:
    """Load and filter the static universe config.

    Parameters
    ----------
    path : Path, optional
        Defaults to qsCrypto/config/universe_coinbase50_binance.yaml.
    pilot_only : bool, default True
        If True, only rows with `pilot: true` are included (the live 3-5
        coin pilot subset). Set False to load the full curated list.
    """
    path = Path(path) if path is not None else DEFAULT_UNIVERSE_PATH
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    rows = raw.get("symbols", [])
    if pilot_only:
        rows = [r for r in rows if r.get("pilot", False)]

    if not rows:
        raise ValueError(
            f"No symbols selected from {path} (pilot_only={pilot_only}) — "
            "check the config has at least one matching row."
        )

    symbols = [r["binance_symbol"] for r in rows]
    generic_of = {r["binance_symbol"]: r["generic"] for r in rows}
    corr_groups = {r["binance_symbol"]: r["corr_group"] for r in rows}
    risk_overrides = {
        r["binance_symbol"]: r.get("risk_overrides") or {} for r in rows
    }

    group_cfg = raw.get("correlation_groups", {})
    max_corr_units = {
        name: cfg.get("max_corr_units") for name, cfg in group_cfg.items()
    }

    return UniverseConfig(
        universe_name=raw.get("universe_name", "unnamed"),
        symbols=symbols,
        generic_of=generic_of,
        corr_groups=corr_groups,
        max_corr_units=max_corr_units,
        risk_overrides=risk_overrides,
    )
