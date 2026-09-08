class Event(object):
    pass


class TickEvent(Event):
    def __init__(self, instrument, time, bid, ask):
        self.type = 'TICK'
        self.instrument = instrument
        self.time = time
        self.bid = bid
        self.ask = ask

    def __str__(self):
        return "Type: %s, Instrument: %s, Time: %s, Bid: %s, Ask: %s" % (
            str(self.type), str(self.instrument), 
            str(self.time), str(self.bid), str(self.ask)
        )

    def __repr__(self):
        return str(self)


class SignalEvent(Event):
    def __init__(self, instrument, order_type, side, time,
                 n=None, level=None, mid=None):
        """`n`, `level` y `mid` viajan con la señal porque el sizing, el stop y el
        objetivo tienen que usar los valores CONGELADOS en la barra que rompió el
        canal, no los de la barra en la que se entra dos barras después. Si el
        portfolio los recalculase al recibir la señal estaría usando información
        distinta de la que generó la decisión.

        Van con defecto `None` para que las estrategias que no los producen
        (TestStrategy, MovingAverageCrossStrategy) sigan construyendo el evento
        con cuatro argumentos posicionales, como hasta ahora.
        """
        self.type = 'SIGNAL'
        self.instrument = instrument
        self.order_type = order_type
        self.side = side
        self.time = time  # Time of the last tick that generated the signal
        self.n = n            # volatilidad N (EMA-TR) de la barra de ruptura
        self.level = level    # nivel del canal roto
        self.mid = mid        # centro del canal: objetivo de la reversión

    def __str__(self):
        return "Type: %s, Instrument: %s, Order Type: %s, Side: %s" % (
            str(self.type), str(self.instrument), 
            str(self.order_type), str(self.side)
        )

    def __repr__(self):
        return str(self)


class OrderEvent(Event):
    def __init__(self, instrument, units, order_type, side):
        self.type = 'ORDER'
        self.instrument = instrument
        self.units = units
        self.order_type = order_type
        self.side = side

    def __str__(self):
        return "Type: %s, Instrument: %s, Units: %s, Order Type: %s, Side: %s" % (
            str(self.type), str(self.instrument), str(self.units),
            str(self.order_type), str(self.side)
        )

    def __repr__(self):
        return str(self)

class BarEvent(Event):
    """Barra OHLC CERRADA.

    La señal de reversión necesita OHLC (canal Donchian, True Range) y el tick de
    bid/ask no lo lleva. Se emite solo con la barra terminada: una barra en curso
    tiene máximo y mínimo provisionales, y actuar sobre ella adelanta la señal a
    información que aún no está completa.
    """
    def __init__(self, instrument, time, open_, high, low, close,
                 volume=0.0, complete=True):
        self.type = 'BAR'
        self.instrument = instrument
        self.time = time
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.complete = complete

    def __str__(self):
        return (
            "Type: %s, Instrument: %s, Time: %s, O: %s, H: %s, L: %s, C: %s" % (
                str(self.type), str(self.instrument), str(self.time),
                str(self.open), str(self.high), str(self.low), str(self.close)
            )
        )

    def __repr__(self):
        return str(self)


class FillEvent(Event):
    """Ejecución CONFIRMADA por el bróker.

    Cierra la rama `execution -> portfolio` del bucle. Sin este evento el
    portfolio actualiza su estado en el momento de EMITIR la orden y nunca se
    entera de a qué precio se llenó de verdad, ni de si se llenó parcialmente,
    ni de si el bróker la rechazó. El precio que importa para el ancla de la
    excursión es este, no el de referencia con el que se decidió.

    `account` es obligatorio en la práctica: el login FA de esta cuenta reparte
    las órdenes entre subcuentas y la etiqueta con la que vuelve el fill no
    siempre es la que se pidió.
    """
    def __init__(self, instrument, units, side, fill_price, commission=0.0,
                 time=None, account=None, order_id=None, is_add=False,
                 exec_id=None):
        self.type = 'FILL'
        self.instrument = instrument
        self.units = units
        self.side = side
        self.fill_price = fill_price
        self.commission = commission
        self.time = time
        self.account = account
        self.order_id = order_id
        self.is_add = is_add
        self.exec_id = exec_id

    def __str__(self):
        return (
            "Type: %s, Instrument: %s, Units: %s, Side: %s, Price: %s, Acct: %s" % (
                str(self.type), str(self.instrument), str(self.units),
                str(self.side), str(self.fill_price), str(self.account)
            )
        )

    def __repr__(self):
        return str(self)


class PortfolioEvent(Event):
    """Foto del portfolio tras un cambio de estado.

    Es lo que alimenta el journal y el monitor sin que ninguno de los dos tenga
    que hurgar en las estructuras internas del Portfolio mientras el hilo de
    precios las está mutando.
    """
    def __init__(self, time, equity, balance, snapshot=None, reason=""):
        self.type = 'PORTFOLIO'
        self.time = time
        self.equity = equity
        self.balance = balance
        self.snapshot = snapshot or []
        self.reason = reason

    def __str__(self):
        return "Type: %s, Time: %s, Balance: %s, Positions: %s, Reason: %s" % (
            str(self.type), str(self.time), str(self.balance),
            len(self.snapshot), str(self.reason)
        )

    def __repr__(self):
        return str(self)
