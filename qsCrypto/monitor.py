"""
monitor.py
==========

Promoted from the `Monitor` class that used to live inline in
qsCrypto/notebooks/Turtle_MR_Intraday_Live.ipynb (cell 8). It was
notebook-embedded because importing a module doesn't trigger
`if __name__ == "__main__"`, so the IBKR live notebook built its own file
handler by hand. For a multi-symbol, always-on Binance deployment that
state needs to survive outside a single notebook kernel, hence this module.

Interface is unchanged from the notebook version: heartbeat(), write(),
record(), record_error(). The only structural change is that SESSION_ID
(previously a module-level global the notebook cell captured via closure)
is now an instance attribute, since a real module has no notebook cell
scope to close over.

Venue-specific fields (specs, account, corr_groups, funding_paid_to_date,
...) are NOT hardcoded here — this class only writes whatever kwargs the
caller passes to write("session_start", ...). The Binance live wiring
script/notebook is responsible for passing the Binance-shaped session-start
payload (precision dict instead of multiplier/expiry, {venue, testnet}
instead of an IBKR account string, corr_groups, funding_paid_to_date) —
see qsCrypto/notebooks/Turtle_MR_Intraday_Live_Binance.ipynb.
"""

from __future__ import print_function

from datetime import datetime, timezone
import json
import logging
import os
import threading


class Monitor(object):
    """Receives PortfolioEvents, writes the journal, and keeps the latest
    snapshot on disk.

    Also the hook trade() (qsCrypto/trading/trading.py) uses for two health
    signals that didn't exist before this class was introduced:
    heartbeat() marks every turn of the event loop (called whether or not
    there was an event to dispatch, so its staleness reveals a hung thread
    even if `threading` still reports it alive), and record_error() counts
    events the engine had to discard because of an exception, instead of
    letting the thread die silently.
    """

    def __init__(self, journal_path, state_path, session_id=None):
        self.journal_path = journal_path
        self.state_path = state_path
        self.session_id = session_id or datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        self.last = []
        self.history = []
        self.errors = []
        self.last_heartbeat = None
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    def heartbeat(self):
        self.last_heartbeat = datetime.now(timezone.utc)

    def record_error(self, event_type):
        with self.lock:
            self.errors.append((datetime.now(timezone.utc), event_type))
        self.write("dispatch_error", event_type=str(event_type))

    def write(self, evento, **extra):
        row = {"ts": datetime.now(timezone.utc).isoformat(),
               "session": self.session_id, "evento": evento}
        row.update(extra)
        with self.lock:
            with open(self.journal_path, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")
                f.flush()
        return row

    def record(self, event):
        """Called by trade() on a PORTFOLIO event."""
        with self.lock:
            self.last = event.snapshot
        self.history.append((datetime.now(timezone.utc), event.reason,
                              len(event.snapshot)))
        self.write("portfolio", reason=event.reason, balance=event.balance,
                   positions=event.snapshot)
        # Atomic write: a reader never sees a half-written JSON file.
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"ts": datetime.now(timezone.utc).isoformat(),
                       "session": self.session_id, "reason": event.reason,
                       "balance": event.balance, "positions": event.snapshot},
                      f, indent=2, default=str)
        os.replace(tmp, self.state_path)
