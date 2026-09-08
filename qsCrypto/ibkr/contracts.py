"""Programmatic front-month resolution for CME crypto futures."""
from datetime import datetime, timezone

from ib_async import Future


def front_month(ib, symbol="MBT", exchange="CME", currency="USD", min_days=2):
    """Return the nearest non-expiring Future contract for `symbol`.

    Expiries are pulled from IB's contract details rather than hardcoded.
    Contracts within `min_days` of expiry are skipped so we never enter a
    position on a contract that is about to stop trading.
    
    Bitcoin can be delivered on line and doesn't have restrictions on storage
    or transfer, hence 2 days to expiry is the front month.
    
    """
    details = ib.reqContractDetails(
        Future(symbol=symbol, exchange=exchange, currency=currency)
    )
    if not details:
        raise RuntimeError(
            f"No contract details for {symbol}@{exchange}. "
            "Check futures permissions on the account."
        )
    today = datetime.now(timezone.utc).date()
    candidates = []
    for d in details:
        c = d.contract
        expiry = datetime.strptime(c.lastTradeDateOrContractMonth[:8], "%Y%m%d").date()
        if (expiry - today).days >= min_days:
            candidates.append((expiry, c, d))
    if not candidates:
        raise RuntimeError(f"No {symbol} contract more than {min_days}d from expiry.")
    candidates.sort(key=lambda t: t[0])
    expiry, contract, detail = candidates[0]
    return contract, detail, expiry
