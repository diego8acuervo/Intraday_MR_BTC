from __future__ import print_function

from qsCrypto.backtest.backtest import Backtest
from qsCrypto.execution.execution import SimulatedExecution
from qsCrypto.portfolio.portfolio import Portfolio
from qsCrypto import settings
from qsCrypto.strategy.strategy import MovingAverageCrossStrategy
from qsCrypto.data.price import HistoricCSVPriceHandler


if __name__ == "__main__":
    # Bitcoin futures on CME
    pairs = ["MBT", "BRR"]
    
    # Create the strategy parameters for the
    # MovingAverageCrossStrategy
    strategy_params = {
        "short_window": 50, 
        "long_window": 200
    }
   
    # Create and execute the backtest
    backtest = Backtest(
        pairs, HistoricCSVPriceHandler, 
        MovingAverageCrossStrategy, strategy_params, 
        Portfolio, SimulatedExecution, 
        equity=settings.EQUITY
    )
    backtest.simulate_trading()