# qsCrypto — Quant Test environment
# `make` (or `make bootstrap`) takes a clean clone to a verified IBKR connection.

SHELL      := /bin/bash
VENV       := .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
PYTHON     ?= python3
OUTPUT_DIR := output
JOURNAL_DIR:= journals

# Python deps not pinned in pyproject.toml (broker + notebook tooling).
EXTRA_DEPS := ib_async nest_asyncio jupyterlab ipywidgets ipympl

.DEFAULT_GOAL := bootstrap
.PHONY: bootstrap venv deps env dirs install check quote accounts positions lab \
        backtest test clean distclean help

## bootstrap: full setup then verify the broker connection (run this first)
bootstrap: install env dirs check

## venv: create the virtualenv if missing
venv: $(PY)
$(PY):
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -q --upgrade pip setuptools wheel

## deps: install third-party dependencies
deps: venv
	$(PIP) install -q $(EXTRA_DEPS)

## install: deps + qsCrypto in editable mode
install: deps
	$(PIP) install -q -e .

## env: create .env from .env.example if it does not exist
env:
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example"; }

## dirs: create gitignored working directories
dirs:
	@mkdir -p $(OUTPUT_DIR) $(JOURNAL_DIR)

## check: connect to TWS read-only, resolve front month, print a quote
check: venv
	$(PY) -m qsCrypto.ibkr.check_connection

## accounts: list managed accounts with balances
accounts: venv
	@$(PY) -c "from ib_async import IB,util; util.patchAsyncio(); \
	from qsCrypto.ibkr import config as c; ib=IB(); \
	ib.connect(c.IB_HOST,c.IB_PORT,clientId=c.IB_CLIENT_ID+80,timeout=10,readonly=True); \
	[print(a, {v.tag:v.value for v in ib.accountSummary(a) if v.tag in ('NetLiquidation','AvailableFunds','BuyingPower')}) for a in ib.managedAccounts()]; \
	ib.disconnect()"

## positions: list open positions across all managed accounts
positions: venv
	@$(PY) -c "from ib_async import IB,util; util.patchAsyncio(); \
	from qsCrypto.ibkr import config as c; ib=IB(); \
	ib.connect(c.IB_HOST,c.IB_PORT,clientId=c.IB_CLIENT_ID+81,timeout=10,readonly=True); \
	ib.reqPositions(); ib.sleep(2); \
	[print(p.account, p.contract.secType, p.contract.localSymbol, p.position, round(p.avgCost,2)) for p in ib.positions()] or print('no positions'); \
	ib.disconnect()"

## quote: print the front-month contract and its current quote
quote: venv
	@$(PY) -c "from ib_async import IB,util; util.patchAsyncio(); \
	from qsCrypto.ibkr import config as c; from qsCrypto.ibkr.contracts import front_month; ib=IB(); \
	ib.connect(c.IB_HOST,c.IB_PORT,clientId=c.IB_CLIENT_ID+82,timeout=10,readonly=True); \
	ib.reqMarketDataType(c.IB_MARKET_DATA_TYPE); \
	ct,_,exp=front_month(ib,c.IB_SYMBOL,c.IB_EXCHANGE,c.IB_CURRENCY); \
	t=ib.reqMktData(ct,'',False,False); ib.sleep(4); \
	print(ct.localSymbol, exp, 'bid',t.bid,'ask',t.ask,'last',t.last,'close',t.close); \
	ib.disconnect()"

## lab: start JupyterLab in the repo root
lab: venv
	$(PY) -m jupyterlab

## backtest: run the inherited moving-average-cross backtest
##   NOTE: needs PAIR_YYYYMMDD.csv tick files in qsCrypto/data (see SETUP.md)
backtest: venv dirs
	QSCRYPTO_CSV_DATA_DIR=$(PWD)/qsCrypto/data \
	QSCRYPTO_OUTPUT_RESULTS_DIR=$(PWD)/$(OUTPUT_DIR) \
	$(PY) -m qsCrypto.examples.mac

## test: run the repo's unit tests
test: venv dirs
	QSCRYPTO_CSV_DATA_DIR=$(PWD)/qsCrypto/data \
	QSCRYPTO_OUTPUT_RESULTS_DIR=$(PWD)/$(OUTPUT_DIR) \
	$(PY) -m unittest discover -s qsCrypto -p "*_test.py" -v

## clean: remove build artefacts and caches
clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf *.egg-info build dist .pytest_cache

## distclean: clean + delete the virtualenv
distclean: clean
	rm -rf $(VENV)

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
