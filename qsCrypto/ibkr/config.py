"""IBKR connection settings, overridable via .env / environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# TWS paper/demo listens on 7496 for the DF* demo accounts used here;
# 7497 is the standard TWS paper port and 4002 the IB Gateway paper port.
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "7496"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "1001"))

# Account to trade. This TWS login is a Financial Advisor demo: DF496956 is the
# FA master (orders placed there are rejected without an allocation) and
# DU496957-61 are the tradeable client accounts. Every order MUST carry an
# explicit order.account, so this is set rather than left blank.
IB_ACCOUNT = os.environ.get("IB_ACCOUNT", "DU496957")

# 1=live, 2=frozen, 3=delayed, 4=delayed-frozen. Delayed is acceptable for
# the exercise but degrades MAE tracking (see NOTES).
IB_MARKET_DATA_TYPE = int(os.environ.get("IB_MARKET_DATA_TYPE", "3"))

# CME crypto futures: MBT = 0.1 BTC/contract, BRR = 5 BTC/contract.
IB_SYMBOL = os.environ.get("IB_SYMBOL", "MBT")
IB_EXCHANGE = os.environ.get("IB_EXCHANGE", "CME")
IB_CURRENCY = os.environ.get("IB_CURRENCY", "USD")
