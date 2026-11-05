"""Calendario de sesiones de un contrato de IBKR, en UTC.

Sale de `ContractDetails.liquidHours` (sesión regular) o `tradingHours`
(extendida) más `timeZoneId`, que IBKR publica para unos siete días vista:

    20260911:0930-20260911:1600;20260912:CLOSED;20260914:0930-20260914:1600

Sin esto el motor no distingue "mercado cerrado" de "feed caído": con una
acción o un CFD sobre acción (IBIT: 09:30-16:00 ET), el watchdog de
`IBKRPriceHandler` reabría suscripciones cada 5 minutos toda la noche y todo
el fin de semana, y la última barra de la sesión no se publicaba hasta la
primera de la sesión siguiente.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# IBKR ha devuelto en distintas versiones nombres abreviados en vez de IANA.
_TZ_ALIASES = {"EST": "US/Eastern", "EST5EDT": "US/Eastern", "CST": "US/Central",
               "CST6CDT": "US/Central", "MST": "US/Mountain", "PST": "US/Pacific",
               "GMT": "UTC", "JST": "Asia/Tokyo", "MET": "Europe/Berlin"}


def _zone(tz_id):
    name = (tz_id or "UTC").split(" ")[0]
    return ZoneInfo(_TZ_ALIASES.get(name, name))


def _to_utc(stamp, tz):
    """'20260911:0930' en la zona del contrato -> datetime UTC naive."""
    local = datetime.strptime(stamp, "%Y%m%d:%H%M").replace(tzinfo=tz)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


class TradingSessions(object):
    """Intervalos [inicio, fin) de sesión, en UTC naive, ordenados."""

    def __init__(self, sessions):
        self.sessions = sorted(sessions)

    @classmethod
    def from_hours(cls, hours, tz_id):
        tz = _zone(tz_id)
        out = []
        for seg in (hours or "").split(";"):
            seg = seg.strip()
            if not seg or seg.endswith("CLOSED") or "-" not in seg:
                continue
            start, end = seg.split("-", 1)
            if ":" not in end:                 # formato viejo: 20090507:0930-1600
                end = start.split(":")[0] + ":" + end
            out.append((_to_utc(start, tz), _to_utc(end, tz)))
        return cls(out)

    @classmethod
    def from_details(cls, detail, use_rth=True):
        hours = detail.liquidHours if use_rth else detail.tradingHours
        return cls.from_hours(hours, detail.timeZoneId)

    def covers(self, now):
        """True si `now` cae dentro del horizonte publicado. Fuera de él el
        calendario no sabe nada y quien lo use debe tratar el mercado como
        abierto (el comportamiento anterior) y refrescarlo."""
        return bool(self.sessions) and now <= self.sessions[-1][1]

    def current(self, now):
        for start, end in self.sessions:
            if start <= now < end:
                return start, end
        return None

    def is_open(self, now):
        return self.current(now) is not None

    def next_open(self, now):
        for start, _ in self.sessions:
            if start > now:
                return start
        return None

    def last_close(self, now):
        closes = [end for _, end in self.sessions if end <= now]
        return closes[-1] if closes else None

    def __len__(self):
        return len(self.sessions)

    def __repr__(self):
        if not self.sessions:
            return "TradingSessions([])"
        return "TradingSessions(%d sesiones, %s -> %s UTC)" % (
            len(self.sessions), self.sessions[0][0], self.sessions[-1][1])


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def floor_to(ts, secs):
    """Inicio del bloque de `secs` segundos que contiene `ts` (alineado a la
    época, que para 30 min coincide con :00/:30 — donde IBKR corta las barras
    de 30 min tanto del CME como de las acciones US)."""
    epoch = datetime(1970, 1, 1)
    return epoch + timedelta(seconds=int((ts - epoch).total_seconds() // secs) * secs)
