"""Smoke test: connect to TWS, report the account, resolve the front month.

    python -m qsCrypto.ibkr.check_connection
"""
from ib_async import IB, util

from qsCrypto.ibkr import config
from qsCrypto.ibkr.contracts import front_month


def main():
    ib = IB()
    print(f"Connecting to {config.IB_HOST}:{config.IB_PORT} "
          f"(clientId={config.IB_CLIENT_ID}) ...")
    ib.connect(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID,
               timeout=10, readonly=True)
    print(f"  connected. server version {ib.client.serverVersion()}, "
          f"time {ib.reqCurrentTime()}")

    accounts = ib.managedAccounts()
    print(f"  managed accounts: {accounts}")
    account = config.IB_ACCOUNT or (accounts[0] if accounts else None)
    if account and account not in accounts:
        raise RuntimeError(f"IB_ACCOUNT={account} not in managed accounts {accounts}")
    if account:
        summary = {v.tag: v.value for v in ib.accountSummary(account)
                   if v.tag in ("NetLiquidation", "AvailableFunds", "BuyingPower")}
        print(f"  account {account}: {summary}")
        ib.reqPositions()
        ib.sleep(2)
        positions = [p for p in ib.positions() if p.account == account]
        print(f"  open positions: "
              f"{[(p.contract.localSymbol, p.position) for p in positions] or 'none'}")
        print(f"  open orders: {len(ib.reqOpenOrders())}")

    ib.reqMarketDataType(config.IB_MARKET_DATA_TYPE)
    contract, detail, expiry = front_month(
        ib, config.IB_SYMBOL, config.IB_EXCHANGE, config.IB_CURRENCY)
    print(f"  front month {config.IB_SYMBOL}: {contract.localSymbol} "
          f"expiry {expiry} conId {contract.conId} "
          f"multiplier {contract.multiplier} tick {detail.minTick}")
    print(f"  trading hours: {detail.tradingHours}")
    print(f"  liquid hours:  {detail.liquidHours}")

    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(4)
    print(f"  quote: bid={ticker.bid} ask={ticker.ask} last={ticker.last} "
          f"close={ticker.close} (marketDataType={config.IB_MARKET_DATA_TYPE})")
    ib.cancelMktData(contract)
    ib.disconnect()
    print("OK")


if __name__ == "__main__":
    util.patchAsyncio()
    main()
