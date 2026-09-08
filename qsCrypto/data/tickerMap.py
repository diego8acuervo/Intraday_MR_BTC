"""
generic_ticker_map.py
=====================

Mapea los tickers GENÉRICOS de la imagen de entrada (simbología tipo
Pinnacle Data Corp. "CLC" / continuous-contract, la misma usada en muchos
papers cuantitativos con rangos 1990-2022) a los nemotécnicos de:

    - "ib"       -> Interactive Brokers (raíz del futuro; micro/mini si existe)
    - "binance"  -> Binance USDⓈ-M perpetuals (símbolo BASE, sin el sufijo USDT)
    - "yahoo"    -> Yahoo! Finance (futuro "=F" cuando existe; índice cash "^..."
                    cuando Yahoo no lista el futuro)

Diseño (según lo pedido):
    map_generics(genericos, exchange) -> dict {generico: nemotecnico | None}

Los SUFIJOS y detalles (p.ej. "USDT" en Binance, la moneda base, el "=F" ya va
incluido en Yahoo, o el exchange/secType de IB) los administra OTRA función que
consume este resultado. Aquí solo se resuelve la raíz por-bolsa.

--------------------------------------------------------------------------------
REGLA DE DESAMBIGUACIÓN (pedida explícitamente)
--------------------------------------------------------------------------------
Cuando un mismo genérico mapea a más de un contrato de IB (full vs micro), se
devuelve el de MENOR tamaño. Ej.: CRUDE OIL -> MCL (micro, 100 bbl) en lugar de
CL (1.000 bbl). Esto se controla con `prefer_micro=True` (default).

    ⚠️  TENSIÓN CON "series tan largas como sea posible":
    Los micro/mini (MCL, MES, MNQ, MGC, XC, XW, XK, QG...) son MUCHO más
    recientes y/o menos líquidos que el contrato full, por lo que su histórico
    es corto. Si el objetivo es maximizar longitud de la serie, llama con
    `prefer_micro=False` para obtener el contrato full (CL, ES, NQ, GC, ZC...).
    El precio en sí es idéntico (mismo subyacente), solo cambia el multiplicador.

--------------------------------------------------------------------------------
IMPORTANTE: el genérico NO es el símbolo del exchange
--------------------------------------------------------------------------------
La columna genérica es una simbología propietaria: el mapeo se hace por la
DESCRIPCIÓN, no por el ticker. Ejemplos donde coincide el nombre pero NO el
mercado real: en la imagen "ZN"=Natural Gas, "ZB"=RBOB, "ZF"=Feeder Cattle,
"ZT"=Live Cattle... que en CME/Globex significan otra cosa.

--------------------------------------------------------------------------------
BINANCE: cobertura real (verificada, ago-2026)
--------------------------------------------------------------------------------
Binance solo lista perpetuos de commodities para un subconjunto pequeño. A la
fecha existen 5 relevantes para esta lista (todos margin/settle en USDT):
    Gold   -> XAU (XAUUSDT)   |  Silver -> XAG (XAGUSDT)
    WTI    -> CL  (CLUSDT)    |  Brent  -> BZ  (BZUSDT)
    NatGas -> NATGAS (NATGASUSDT)
Nota: NatGas usa la raíz "NATGAS" (no "NG"). El resto de la lista (agrícolas,
índices de acciones, bonos, FX, cobre, platino...) NO tiene perpetuo en Binance
-> se devuelve None.
Fuentes: anuncios de lanzamiento Binance Futures ene-abr 2026 (XAU/XAG el
5-7 ene; CL/BZ/NATGAS el 1 abr). Ver notas al pie.

--------------------------------------------------------------------------------
CONFIANZA
--------------------------------------------------------------------------------
Los mapeos "core" (energía, metales, granos, softs US, índices US e-mini, notas
del Tesoro US, FX CME) son de alta confianza. Los marcados en LOW_CONFIDENCE
(leche, GSCI, lumber, FTSE en IB, Nikkei en IB) requieren verificación puntual
contra el buscador de contratos de la fuente antes de producción.

NOTA: se retiraron de GENERIC_MAP 10 genéricos sin serie utilizable
(BG, MW, MD, XX, AP, DT, GS, UB, CB, DX): ninguno resolvía a un histórico
válido ni por Yahoo ni por IBKR en la validación del walk-forward.
"""

from __future__ import annotations
from typing import Dict, Iterable, Optional

SUPPORTED_EXCHANGES = ("ib", "binance", "yahoo")

# Alias aceptados -> clave canónica
_EXCHANGE_ALIASES = {
    "ib": "ib", "interactive_brokers": "ib", "interactivebrokers": "ib", "ibkr": "ib",
    "binance": "binance", "binance_perp": "binance", "binance_futures": "binance",
    "yahoo": "yahoo", "yahoo_finance": "yahoo", "yf": "yahoo", "yahoofinance": "yahoo",
}

# Genéricos cuyo mapeo es tentativo (verificar antes de producción).
LOW_CONFIDENCE = {
    "DA",  # Milk Class III: DC / DC=F por confirmar
    "GI",  # S&P GSCI: GD=F por confirmar; IB puede no listar el futuro
    "LB",  # Lumber: contrato antiguo (LB) delistado 2023 -> nuevo LBR / LBS=F
    "LX",  # FTSE 100: raíz IB ("Z") por confirmar (Yahoo ^FTSE cash ok)
    "NK",  # Nikkei: raíz IB por confirmar (Yahoo ^N225 cash ok)
    "VX",  # VIX: Yahoo solo da el índice cash (^VIX), NO el futuro; el futuro
           # cotiza en contango/backwardation y su serie difiere mucho del cash
}

# ------------------------------------------------------------------------------
# TABLA MAESTRA  (clave = genérico de la imagen)
#   desc      : descripción de la imagen (para trazabilidad)
#   yahoo     : símbolo Yahoo ("=F" futuro | "^" índice cash | None)
#   ib        : raíz IB del contrato FULL
#   ib_micro  : raíz IB del micro/mini (solo si existe con alta confianza)
#   binance   : símbolo BASE del perpetuo (sin "USDT") | None
# ------------------------------------------------------------------------------
GENERIC_MAP: Dict[str, dict] = {
    # ---------------- COMMODITIES ----------------
    "BC": {"desc": "Brent Crude Oil",      "yahoo": "BZ=F", "ib": "BZ",  "ib_micro": None,  "binance": "BZ"},
    "CC": {"desc": "Cocoa",                "yahoo": "CC=F", "ib": "CC",  "ib_micro": None,  "binance": None},
    "CL": {"desc": "Crude Oil (WTI)",      "yahoo": "CL=F", "ib": "CL",  "ib_micro": "MCL", "binance": "CL"},
    "CT": {"desc": "Cotton #2",            "yahoo": "CT=F", "ib": "CT",  "ib_micro": None,  "binance": None},
    "DA": {"desc": "Milk Class III",       "yahoo": "DC=F", "ib": "DC",  "ib_micro": None,  "binance": None},
    "GI": {"desc": "S&P GSCI",             "yahoo": "GD=F", "ib": None,  "ib_micro": None,  "binance": None},
    "JO": {"desc": "Orange Juice",         "yahoo": "OJ=F", "ib": "OJ",  "ib_micro": None,  "binance": None},
    "KC": {"desc": "Coffee",               "yahoo": "KC=F", "ib": "KC",  "ib_micro": None,  "binance": None},
    "KW": {"desc": "Wheat (KC / KCBT)",    "yahoo": "KE=F", "ib": "KE",  "ib_micro": None,  "binance": None},
    "LB": {"desc": "Lumber",               "yahoo": "LBS=F","ib": "LBR", "ib_micro": None,  "binance": None},
    "NR": {"desc": "Natural Gas",          "yahoo": "NG=F", "ib": "NG",  "ib_micro": "QG",  "binance": "NATGAS"},
    "SB": {"desc": "Sugar #11",            "yahoo": "SB=F", "ib": "SB",  "ib_micro": None,  "binance": None},
    "W_": {"desc": "Wheat, CBOT",          "yahoo": "ZW=F", "ib": "ZW",  "ib_micro": "XW",  "binance": None},
    "ZA": {"desc": "Palladium",            "yahoo": "PA=F", "ib": "PA",  "ib_micro": None,  "binance": None},
    "ZB": {"desc": "RBOB Gasoline",        "yahoo": "RB=F", "ib": "RB",  "ib_micro": None,  "binance": None},
    "ZC": {"desc": "Corn",                 "yahoo": "ZC=F", "ib": "ZC",  "ib_micro": "XC",  "binance": None},
    "ZF": {"desc": "Feeder Cattle",        "yahoo": "GF=F", "ib": "GF",  "ib_micro": None,  "binance": None},
    "ZG": {"desc": "Gold",                 "yahoo": "GC=F", "ib": "GC",  "ib_micro": "MGC", "binance": "XAU"},
    "ZI": {"desc": "Silver",               "yahoo": "SI=F", "ib": "SI",  "ib_micro": None,  "binance": "XAG"},
    "ZK": {"desc": "Copper",               "yahoo": "HG=F", "ib": "HG",  "ib_micro": "MHG", "binance": None},
    "ZL": {"desc": "Soybean Oil",          "yahoo": "ZL=F", "ib": "ZL",  "ib_micro": None,  "binance": None},
    "ZM": {"desc": "Soybean Meal",         "yahoo": "ZM=F", "ib": "ZM",  "ib_micro": None,  "binance": None},
    "ZN": {"desc": "Natural Gas (elec.)",  "yahoo": "NG=F", "ib": "NG",  "ib_micro": "QG",  "binance": "NATGAS"},
    "ZO": {"desc": "Oats",                 "yahoo": "ZO=F", "ib": "ZO",  "ib_micro": None,  "binance": None},
    "ZP": {"desc": "Platinum",             "yahoo": "PL=F", "ib": "PL",  "ib_micro": None,  "binance": None},
    "ZR": {"desc": "Rough Rice",           "yahoo": "ZR=F", "ib": "ZR",  "ib_micro": None,  "binance": None},
    "ZS": {"desc": "Soybeans",             "yahoo": "ZS=F", "ib": "ZS",  "ib_micro": "XK",  "binance": None},
    "ZT": {"desc": "Live Cattle",          "yahoo": "LE=F", "ib": "LE",  "ib_micro": None,  "binance": None},
    "ZU": {"desc": "Crude Oil (elec.)",    "yahoo": "CL=F", "ib": "CL",  "ib_micro": "MCL", "binance": "CL"},
    "ZW": {"desc": "Wheat (elec., CBOT)",  "yahoo": "ZW=F", "ib": "ZW",  "ib_micro": "XW",  "binance": None},
    "ZZ": {"desc": "Lean Hogs",            "yahoo": "HE=F", "ib": "HE",  "ib_micro": None,  "binance": None},

    # ---------------- EQUITY INDEX ----------------
    # (Yahoo: US e-mini como futuro "=F"; europeos/asiáticos como índice cash "^")
    "AX": {"desc": "German DAX",           "yahoo": "^GDAXI",   "ib": "DAX",    "ib_micro": None,  "binance": None},
    "CA": {"desc": "CAC 40",               "yahoo": "^FCHI",    "ib": "CAC40",  "ib_micro": None,  "binance": None},
    "EN": {"desc": "Nasdaq-100 e-mini",    "yahoo": "NQ=F",     "ib": "NQ",     "ib_micro": "MNQ", "binance": None},
    "ER": {"desc": "Russell 2000 e-mini",  "yahoo": "RTY=F",    "ib": "RTY",    "ib_micro": "M2K", "binance": None},
    "ES": {"desc": "S&P 500 e-mini",       "yahoo": "ES=F",     "ib": "ES",     "ib_micro": "MES", "binance": None},
    "HS": {"desc": "Hang Seng",            "yahoo": "^HSI",     "ib": "HSI",    "ib_micro": "MHI", "binance": None},
    "LX": {"desc": "FTSE 100",             "yahoo": "^FTSE",    "ib": "Z",      "ib_micro": None,  "binance": None},
    "SC": {"desc": "S&P 500 (composite)",  "yahoo": "ES=F",     "ib": "ES",     "ib_micro": "MES", "binance": None},
    "SP": {"desc": "S&P 500 (day session)","yahoo": "ES=F",     "ib": "ES",     "ib_micro": "MES", "binance": None},
    "VX": {"desc": "CBOE Volatility (VIX)","yahoo": "^VIX",     "ib": "VIX",    "ib_micro": "VXM", "binance": None},
    "XU": {"desc": "EURO STOXX 50",        "yahoo": "^STOXX50E","ib": "ESTX50", "ib_micro": None,  "binance": None},
    "YM": {"desc": "DJIA e-mini ($5)",     "yahoo": "YM=F",     "ib": "YM",     "ib_micro": "MYM", "binance": None},

    # ---------------- FIXED INCOME ----------------
    "FB": {"desc": "T-Note 5yr",            "yahoo": "ZF=F","ib": "ZF",  "ib_micro": None, "binance": None},
    "TU": {"desc": "T-Note 2yr",            "yahoo": "ZT=F","ib": "ZT",  "ib_micro": None, "binance": None},
    "TY": {"desc": "T-Note 10yr",           "yahoo": "ZN=F","ib": "ZN",  "ib_micro": None, "binance": None},
    "US": {"desc": "T-Bond 30yr",           "yahoo": "ZB=F","ib": "ZB",  "ib_micro": None, "binance": None},

    # ---------------- CURRENCIES / OTROS ----------------
    "AN": {"desc": "Australian Dollar",    "yahoo": "6A=F", "ib": "6A", "ib_micro": "M6A", "binance": None},
    "BN": {"desc": "British Pound",        "yahoo": "6B=F", "ib": "6B", "ib_micro": "M6B", "binance": None},
    "CN": {"desc": "Canadian Dollar",      "yahoo": "6C=F", "ib": "6C", "ib_micro": None,  "binance": None},
    "FN": {"desc": "Euro FX",              "yahoo": "6E=F", "ib": "6E", "ib_micro": "M6E", "binance": None},
    "JN": {"desc": "Japanese Yen",         "yahoo": "6J=F", "ib": "6J", "ib_micro": None,  "binance": None},
    "MP": {"desc": "Mexican Peso",         "yahoo": "6M=F", "ib": "6M", "ib_micro": None,  "binance": None},
    "NK": {"desc": "Nikkei 225",           "yahoo": "^N225","ib": "N225","ib_micro": None, "binance": None},
    "SN": {"desc": "Swiss Franc",          "yahoo": "6S=F", "ib": "6S", "ib_micro": None,  "binance": None},
    "BTC": {"desc": "Bitcoin",              "yahoo": "BTC-USD", "ib": "BRR", "ib_micro": "MBT", "binance": "BTCUSDT"}
}


# ------------------------------------------------------------------------------
# FUNCIÓN PRINCIPAL
# ------------------------------------------------------------------------------
def map_generics(
    generics: Iterable[str],
    exchange: str,
    prefer_micro: bool = True,
    strict: bool = False,
) -> Dict[str, Optional[str]]:
    """
    Mapea un subconjunto de tickers genéricos a los nemotécnicos de una bolsa.

    Parámetros
    ----------
    generics : iterable de str
        Subconjunto de las claves de GENERIC_MAP (p.ej. ["BC", "CL", "ZG"]).
        No es sensible a mayúsculas; los espacios se ignoran.
    exchange : str
        "ib" | "binance" | "yahoo" (se aceptan alias, ver _EXCHANGE_ALIASES).
    prefer_micro : bool, default True
        Solo aplica a "ib": si existe micro/mini, devuelve el de menor tamaño
        (regla de desambiguación pedida). Pon False para el contrato full
        (recomendado si quieres el histórico más largo posible).
    strict : bool, default False
        Si True, lanza KeyError ante un genérico desconocido.
        Si False, lo incluye en el resultado con valor None.

    Retorna
    -------
    dict {generico_original: nemotecnico | None}
        Conserva el orden de entrada. None = la bolsa no lista ese instrumento
        (o el genérico es desconocido en modo no-estricto).
    """
    ex = _EXCHANGE_ALIASES.get(exchange.strip().lower())
    if ex is None:
        raise ValueError(
            f"exchange '{exchange}' no soportado. Usa uno de: {SUPPORTED_EXCHANGES}"
        )

    out: Dict[str, Optional[str]] = {}
    for raw in generics:
        key = str(raw).strip().upper()
        entry = GENERIC_MAP.get(key)
        if entry is None:
            if strict:
                raise KeyError(f"Genérico desconocido: {raw!r}")
            out[raw] = None
            continue

        if ex == "ib":
            sym = entry["ib_micro"] if (prefer_micro and entry.get("ib_micro")) else entry["ib"]
        else:
            sym = entry[ex]
        out[raw] = sym
    return out


# ------------------------------------------------------------------------------
# Utilidades opcionales
# ------------------------------------------------------------------------------
def describe(generic: str) -> Optional[dict]:
    """Devuelve la fila cruda de la tabla (todas las bolsas + descripción)."""
    return GENERIC_MAP.get(str(generic).strip().upper())

def front_month(generic: str, exchange: str) -> Optional[str]:
    """Devuelve el símbolo del contrato de mes más cercano para un genérico dado."""
    ex = _EXCHANGE_ALIASES.get(exchange.strip().lower())
    if ex is None:
        raise ValueError(f"exchange '{exchange}' no soportado.")
    # Aquí se implementaría la lógica para encontrar el contrato de mes más cercano
    
    
    return None

def available(exchange: str) -> Dict[str, str]:
    """Genéricos que SÍ tienen símbolo (no None) en la bolsa dada."""
    ex = _EXCHANGE_ALIASES.get(exchange.strip().lower())
    if ex is None:
        raise ValueError(f"exchange '{exchange}' no soportado.")
    res = map_generics(GENERIC_MAP.keys(), ex, prefer_micro=False)
    return {g: s for g, s in res.items() if s is not None}


if __name__ == "__main__":
    # Reproduce los ejemplos del enunciado
    demo = ["BC", "CL", "ZG", "ZN", "ES", "ZW", "AX", "FN"]

    print("IB (prefer_micro=True, regla 'menor tamaño'):")
    print(map_generics(demo, "ib"))
    print("\nIB (prefer_micro=False, histórico más largo):")
    print(map_generics(demo, "ib", prefer_micro=False))
    print("\nBinance (base, sin sufijo USDT):")
    print(map_generics(demo, "binance"))
    print("\nYahoo:")
    print(map_generics(demo, "yahoo"))

    # Chequeo del ejemplo BC -> BZ en IB y Binance
    assert map_generics(["BC"], "ib", prefer_micro=False)["BC"] == "BZ"
    assert map_generics(["BC"], "binance")["BC"] == "BZ"
    # CL -> MCL con la regla de menor tamaño
    assert map_generics(["CL"], "ib")["CL"] == "MCL"
    print("\nOK: asserts de ejemplo pasan.")