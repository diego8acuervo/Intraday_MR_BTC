"""Contract resolution: CME crypto futures (front month) and CFDs/stocks."""
from datetime import datetime, timezone

from ib_async import Contract, Future


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


def resolve_instrument(ib, symbol, sec_type="FUT", exchange="CME",
                       currency="USD", min_days=2):
    """Contrato para OPERAR y contrato para las BARRAS de un símbolo.

    Devuelve (trade_contract, trade_detail, data_contract, data_detail, expiry).

    * FUT: el front month de `front_month`; opera y da barras el mismo
      contrato.
    * CFD: el CFD opera, pero IBKR no sirve histórico para CFDs (medido con
      IBIT: TRADES, MIDPOINT y BID_ASK devuelven vacío), así que las barras
      salen del SUBYACENTE, resuelto por `underConId` del propio CFD en vez de
      suponer su bolsa primaria. El precio de un CFD sobre acción replica el
      de la acción; un fade calibrado sobre barras TRADES necesita esas
      mismas barras.
    * STK: la acción opera y da barras.
    * CASH: contado de divisas en IDEALPRO (`symbol="EURUSD"`); opera y da
      barras el mismo contrato.

    `exchange` para CFD/STK debería ser "SMART": el enrutador elige la bolsa
    por orden, y el histórico de la acción sale consolidado.
    """
    sec_type = sec_type.upper()
    if sec_type == "FUT":
        contract, detail, expiry = front_month(
            ib, symbol, exchange, currency, min_days=min_days)
        return contract, detail, contract, detail, expiry

    if sec_type == "CASH":
        # Contado de divisas: `symbol` es el par ("EURUSD" o "EUR.USD"); la
        # divisa base va en `symbol` del contrato y la cotizada en
        # `currency`. No hay vencimiento ni subyacente: opera y da barras el
        # mismo contrato (barras con whatToShow="MIDPOINT": TRADES vuelve
        # vacío en divisas).
        pair = symbol.replace(".", "").upper()
        if len(pair) != 6:
            raise ValueError(f"Par de divisas no reconocido: {symbol!r}")
        spec = Contract(secType="CASH", symbol=pair[:3], currency=pair[3:],
                        exchange=exchange if exchange not in ("", "SMART") else "IDEALPRO")
    else:
        spec = Contract(secType=sec_type, symbol=symbol, exchange=exchange,
                        currency=currency)
    details = ib.reqContractDetails(spec)
    if not details:
        raise RuntimeError(
            f"Sin contract details para {sec_type} {symbol}@{exchange} "
            f"{currency}. Revisa los permisos de {sec_type} de la cuenta.")
    if len(details) > 1:
        raise RuntimeError(
            f"{sec_type} {symbol}@{exchange} es ambiguo ({len(details)} "
            f"contratos): {[d.contract.localSymbol for d in details]}")
    detail = details[0]
    contract = detail.contract
    if sec_type != "CFD":
        return contract, detail, contract, detail, None

    if not detail.underConId:
        raise RuntimeError(f"El CFD {contract.localSymbol} no publica underConId.")
    under = ib.reqContractDetails(Contract(conId=detail.underConId))
    if not under:
        raise RuntimeError(
            f"Sin contract details del subyacente conId={detail.underConId} "
            f"de {contract.localSymbol}.")
    data_detail = under[0]
    data_contract = data_detail.contract
    # El histórico consolidado de la acción se pide por SMART, no por la
    # bolsa primaria que devuelve el conId.
    if data_contract.secType == "STK":
        data_contract.exchange = "SMART"
    return contract, detail, data_contract, data_detail, None
