import copy
from decimal import Decimal, getcontext
import logging
import logging.config
import os
try:
    import Queue as queue
except ImportError:
    import queue
import threading
import time

from qsCrypto.execution.execution import create_execution_handler
from qsCrypto.portfolio.portfolio import Portfolio
from qsCrypto import settings
from qsCrypto.strategy.strategy import TestStrategy
from qsCrypto.data.streaming import StreamingCryptoPrices

logger = logging.getLogger('qsCrypto.trading.trading')


def trade(events, strategy, portfolio, execution, heartbeat, monitor=None):
    """
    Carries out an infinite while loop that polls the 
    events queue and directs each event to either the
    strategy component of the execution handler. The
    loop will then pause for "heartbeat" seconds and
    continue.

    `monitor` es opcional y con defecto None para no alterar la llamada que ya
    hace el __main__ de Binance más abajo.
    """
    while True:
        try:
            event = events.get(False)
        except queue.Empty:
            pass
        else:
            if event is not None:
                if event.type == 'TICK':
                    logger.info("Received new tick event: %s", event)
                    strategy.calculate_signals(event)
                    portfolio.update_portfolio(event)
                elif event.type == 'BAR':
                    # La señal se calcula sobre barras cerradas; el portfolio
                    # evalúa sobre ellas las barreras de objetivo y vertical.
                    logger.info("Received new bar event: %s", event)
                    strategy.calculate_signals(event)
                    if hasattr(portfolio, 'on_bar'):
                        portfolio.on_bar(event)
                elif event.type == 'SIGNAL':
                    logger.info("Received new signal event: %s", event)
                    portfolio.execute_signal(event)
                elif event.type == 'ORDER':
                    logger.info("Received new order event: %s", event)
                    execution.execute_order(event)
                elif event.type == 'FILL':
                    # La vuelta del bucle: lo que el bróker ejecutó de verdad
                    # regresa al portfolio, que hasta aquí solo sabía lo que
                    # había pedido.
                    logger.info("Received new fill event: %s", event)
                    if hasattr(portfolio, 'update_fill'):
                        portfolio.update_fill(event)
                elif event.type == 'PORTFOLIO':
                    logger.info("Received new portfolio event: %s", event)
                    if monitor is not None:
                        monitor.record(event)
        time.sleep(heartbeat)


if __name__ == "__main__":
    # Set up logging
    log_conf_path = os.path.join(
        os.path.dirname(__file__), '..', 'logging.conf'
    )
    logging.config.fileConfig(log_conf_path)

    # Set the number of decimal places to 2
    getcontext().prec = 2

    heartbeat = 0.5  # Time in seconds between polling
    events = queue.Queue()
    equity = settings.EQUITY

    # Pairs to include in streaming data set
    pairs = ["BTCUSDT", "ETHUSDT"]

    # Create the Binance market price streaming class
    prices = StreamingCryptoPrices(
        pairs=pairs,
        events_queue=events,
        test_mode=settings.TEST_MODE,
        market_type=settings.BINANCE_MARKET_TYPE,
    )

    # Create the strategy/signal generator, passing the
    # instrument and the events queue
    strategy = TestStrategy(pairs, events)

    # Create the portfolio object
    portfolio = Portfolio(
        prices, events, home_currency="USDT",
        equity=equity, backtest=False
    )

    # Create the execution handler
    execution = create_execution_handler(
        test_mode=settings.TEST_MODE,
        market_type=settings.BINANCE_MARKET_TYPE,
    )
    
    # Create two separate threads: One for the trading loop
    # and another for the market price streaming class
    trade_thread = threading.Thread(
        target=trade, args=(
            events, strategy, portfolio, execution, heartbeat
        )
    )
    price_thread = threading.Thread(target=prices.stream_to_queue, args=[])
    
    # Start both threads
    logger.info("Starting trading thread")
    trade_thread.start()
    logger.info("Starting price streaming thread")
    price_thread.start()

