"""
run_pilot_validation.py
========================

Independent validation of the Donchian mean-reversion fade strategy on
Binance USD-M perpetuals, for the 5-coin pilot universe
(qsCrypto/config/universe_coinbase50_binance.yaml). Follows the backtester
skill's workflow: baseline -> walk-forward with purge/embargo -> bias
quantification -> DSR/PBO -> checklist verdict. Run directly:

    python -m qsCrypto.backtest.run_pilot_validation

Prints every section's numbers and writes a JSON summary to
output/binance_perp_validation.json for the notebook to render.
"""
from __future__ import annotations

import json
import sys
import warnings

import numpy as np
import pandas as pd

from qsCrypto.backtest.binance_data import fetch_pilot_universe
from qsCrypto.backtest.engine import BacktestParams, run_backtest
from qsCrypto.backtest.skill_stats.bias_audit import (
    assert_no_lookahead, cost_sensitivity, effective_bets, equity_metrics,
    mc_bootstrap_trades, pnl_concentration,
)
from qsCrypto.backtest.skill_stats.purged_cv import walk_forward_folds
from qsCrypto.backtest.skill_stats.stats_tests import (
    deflated_sharpe_ratio, fold_ttest, pbo_cscv, sharpe_ratio,
)
from qsCrypto.strategy.strategy import compute_n

warnings.filterwarnings("ignore", category=FutureWarning)

PILOT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "LINKUSDT"]
FLAGSHIP = "BTCUSDT"
FREQ = 365  # crypto trades 24/7 — see equity_metrics docstring

BASELINE = BacktestParams()  # canonical Turtle defaults, un-optimized

GRID = [
    {"entry_bars": eb, "stop_n": sn}
    for eb in (15, 20, 25)
    for sn in (1.0, 1.5)
]


def _params_with(overrides):
    d = dict(BASELINE.__dict__)
    d.update(overrides)
    return BacktestParams(**d)


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main():
    out = {}

    section("1. Data")
    data, funding = fetch_pilot_universe(PILOT_SYMBOLS, interval="30m", start="2022-01-01")
    for s in PILOT_SYMBOLS:
        print(f"  {s}: {len(data[s])} bars, {data[s].index[0]} -> {data[s].index[-1]} "
              f"| {len(funding[s])} funding settlements")
    out["universe"] = {
        s: {"bars": len(data[s]), "start": str(data[s].index[0]), "end": str(data[s].index[-1])}
        for s in PILOT_SYMBOLS
    }

    section("2. Anti-look-ahead truncation test (compute_n + Donchian channel)")
    df0 = data[FLAGSHIP]

    def n_feature(d):
        return pd.Series(compute_n(d["high"].to_numpy(), d["low"].to_numpy(),
                                    d["close"].to_numpy(), period=BASELINE.atr_period),
                          index=d.index)

    def channel_feature(d):
        return d["high"].rolling(BASELINE.entry_bars).max().shift(1)

    res_n = assert_no_lookahead(n_feature, df0, n_checks=30)
    res_ch = assert_no_lookahead(channel_feature, df0, n_checks=30)
    print(f"  compute_n: {res_n.ok.sum()}/{len(res_n)} truncation checks pass")
    print(f"  channel  : {res_ch.ok.sum()}/{len(res_ch)} truncation checks pass")
    out["lookahead_test"] = {"compute_n_pass": bool(res_n.ok.all()),
                              "channel_pass": bool(res_ch.ok.all())}

    section("3. Baseline (un-optimized canonical parameters), per symbol")
    baseline_results = {}
    for s in PILOT_SYMBOLS:
        ec, trades = run_backtest(data[s], BASELINE, funding=funding[s])
        m = equity_metrics(ec, trades, freq=FREQ)
        baseline_results[s] = {"ec": ec, "trades": trades, "metrics": m}
        print(f"  {s}: CAGR={m.get('CAGR', float('nan')):.2%} "
              f"Sharpe={m.get('Sharpe', float('nan')):.2f} "
              f"MaxDD={m.get('Max DD', float('nan')):.2%} "
              f"Trades={m.get('Trades', 0)} "
              f"WinRate={m.get('Win rate', float('nan')):.2%}")
    out["baseline"] = {s: {k: v for k, v in r["metrics"].items()}
                        for s, r in baseline_results.items()}

    section("3b. Joint pilot portfolio (equity split evenly, summed curves)")
    equity_share = BASELINE.equity0 / len(PILOT_SYMBOLS)
    joint_params = _params_with({"equity0": equity_share})
    joint_ecs = {}
    for s in PILOT_SYMBOLS:
        ec, _ = run_backtest(data[s], joint_params, funding=funding[s])
        joint_ecs[s] = ec
    joint_df = pd.DataFrame(joint_ecs).ffill().dropna()
    joint_ec = joint_df.sum(axis=1)
    joint_m = equity_metrics(joint_ec)
    print(f"  Joint (5 coins, {equity_share:.0f} USDT each): CAGR={joint_m.get('CAGR', float('nan')):.2%} "
          f"Sharpe={joint_m.get('Sharpe', float('nan')):.2f} MaxDD={joint_m.get('Max DD', float('nan')):.2%}")
    out["joint_portfolio"] = joint_m

    section(f"4. Walk-forward on {FLAGSHIP} — grid of {len(GRID)} configs, "
            "penalized IS selection (Sharpe_IS - 0.5*|MaxDD_IS|)")
    folds = walk_forward_folds(df0.index, is_days=365, oos_days=90, embargo_days=2)
    print(f"  {len(folds)} folds (is=365d, oos=90d, embargo=2d)")

    def run_grid_config(cfg, symbol_df, symbol_funding):
        params = _params_with(cfg)
        return run_backtest(symbol_df, params, funding=symbol_funding)

    fold_rows = []
    oos_returns_selected, oos_returns_baseline = [], []
    winners = []
    for f in folds:
        is_df = df0[(df0.index >= f["is_start"]) & (df0.index < f["is_end"])]
        oos_df = df0[(df0.index >= f["oos_start"]) & (df0.index < f["oos_end"])]
        if len(is_df) < 200 or len(oos_df) < 50:
            continue
        is_fund = funding[FLAGSHIP][(funding[FLAGSHIP].index >= f["is_start"]) & (funding[FLAGSHIP].index < f["is_end"])]
        oos_fund = funding[FLAGSHIP][(funding[FLAGSHIP].index >= f["oos_start"]) & (funding[FLAGSHIP].index < f["oos_end"])]

        best_cfg, best_score = None, -np.inf
        for cfg in GRID:
            ec_is, _ = run_grid_config(cfg, is_df, is_fund)
            m_is = equity_metrics(ec_is, freq=FREQ)
            score = m_is.get("Sharpe", 0) - 0.5 * abs(m_is.get("Max DD", 0))
            if score > best_score:
                best_score, best_cfg = score, cfg
        winners.append(tuple(sorted(best_cfg.items())))

        ec_oos_sel, _ = run_grid_config(best_cfg, oos_df, oos_fund)
        ec_oos_base, _ = run_backtest(oos_df, BASELINE, funding=oos_fund)
        r_sel = ec_oos_sel.pct_change().dropna()
        r_base = ec_oos_base.pct_change().dropna()
        common = r_sel.index.intersection(r_base.index)
        oos_returns_selected.append(r_sel.loc[common])
        oos_returns_baseline.append(r_base.loc[common])

        m_sel = equity_metrics(ec_oos_sel, freq=FREQ)
        m_base = equity_metrics(ec_oos_base, freq=FREQ)
        fold_rows.append({"fold": f["fold"], "winner": best_cfg,
                           "oos_sharpe_selected": m_sel.get("Sharpe", np.nan),
                           "oos_sharpe_baseline": m_base.get("Sharpe", np.nan)})
        print(f"  fold {f['fold']}: IS {f['is_start'].date()}->{f['is_end'].date()} "
              f"OOS {f['oos_start'].date()}->{f['oos_end'].date()} winner={best_cfg} "
              f"OOS Sharpe sel={m_sel.get('Sharpe', np.nan):.2f} base={m_base.get('Sharpe', np.nan):.2f}")

    out["walk_forward"] = {"n_folds": len(fold_rows), "folds": fold_rows}

    if fold_rows:
        oos_sel_concat = pd.concat(oos_returns_selected) if oos_returns_selected else pd.Series(dtype=float)
        oos_base_concat = pd.concat(oos_returns_baseline) if oos_returns_baseline else pd.Series(dtype=float)
        sel_sharpes = [r["oos_sharpe_selected"] for r in fold_rows]
        base_sharpes = [r["oos_sharpe_baseline"] for r in fold_rows]
        tt = fold_ttest(sel_sharpes, base_sharpes)
        print(f"\n  fold t-test (selected vs baseline OOS Sharpe): {tt}")
        out["fold_ttest"] = tt

        from collections import Counter
        winner_counts = Counter(winners)
        mode_winner, mode_count = winner_counts.most_common(1)[0]
        print(f"  parameter stability: mode winner {dict(mode_winner)} chosen in "
              f"{mode_count}/{len(winners)} folds")
        out["param_stability"] = {"mode_winner": dict(mode_winner),
                                   "mode_count": mode_count, "n_folds": len(winners)}
    else:
        oos_sel_concat = pd.Series(dtype=float)
        print("  Not enough history for any fold — sample too short for this WFO window.")

    section("5. Deflated Sharpe Ratio + PBO (n_trials = grid size)")
    trial_sharpes = []
    grid_returns = {}
    for cfg in GRID:
        ec, _ = run_backtest(df0, _params_with(cfg), funding=funding[FLAGSHIP])
        m = equity_metrics(ec, freq=FREQ)
        trial_sharpes.append(m.get("Sharpe", 0.0))
        grid_returns[str(cfg)] = ec.pct_change().dropna()

    if len(oos_sel_concat) > 10:
        dsr = deflated_sharpe_ratio(oos_sel_concat, n_trials=len(GRID),
                                     trial_sharpes=np.array(trial_sharpes), freq=FREQ)
        print(f"  DSR (on concatenated WFO OOS returns): {dsr}")
        out["dsr"] = dsr
    else:
        print("  Not enough OOS data to compute DSR.")
        out["dsr"] = None

    perf = pd.DataFrame(grid_returns).dropna(how="all")
    try:
        pbo = pbo_cscv(perf, n_partitions=10, freq=FREQ)
        print(f"  PBO (CSCV, full-sample grid returns): pbo={pbo['pbo']:.3f} "
              f"({pbo['veredicto']})")
        out["pbo"] = {k: v for k, v in pbo.items() if k != "logits"}
    except Exception as exc:
        print(f"  PBO could not be computed: {exc}")
        out["pbo"] = {"error": str(exc)}

    section("6. Bias quantification")
    base_trades = baseline_results[FLAGSHIP]["trades"]

    print("  6.1 P&L concentration:")
    if len(base_trades):
        conc = pnl_concentration(base_trades)
        print(f"    top_3={conc.get('top_3', float('nan')):.1%} "
              f"top_5={conc.get('top_5', float('nan')):.1%} -> {conc.get('veredicto')}")
        out["pnl_concentration"] = {k: v for k, v in conc.items()
                                     if k not in ("mejores", "peores")}
    else:
        print("    no closed trades on the flagship symbol.")
        out["pnl_concentration"] = None

    print("  6.2 Effective independent bets across the 5-coin pilot:")
    eb = effective_bets({s: data[s] for s in PILOT_SYMBOLS})
    print(f"    {eb.get('veredicto')} (n_efectivo={eb.get('n_efectivo', float('nan')):.2f}, "
          f"corr_media={eb.get('corr_media', float('nan')):.2f})")
    out["effective_bets"] = {k: v for k, v in eb.items() if k != "matriz"}

    print("  6.3 Cost sensitivity (flagship symbol, baseline params):")

    def run_fn(fee, slip, funding_daily):
        params = _params_with({"fee_bps": fee, "slip_bps": slip})
        synth_funding = pd.Series(funding_daily / 3.0, index=funding[FLAGSHIP].index)
        return run_backtest(df0, params, funding=synth_funding)

    cs = cost_sensitivity(run_fn)
    print(cs.to_string(index=False))
    out["cost_sensitivity"] = cs.to_dict(orient="records")
    out["cost_sensitivity_attrs"] = {
        "friccion_cagr": cs.attrs.get("friccion_cagr"),
        "sobrevive_pesimista": cs.attrs.get("sobrevive_pesimista"),
    }

    print("  6.4 Monte Carlo bootstrap of flagship trades:")
    if len(base_trades) >= 10:
        mc = mc_bootstrap_trades(base_trades, equity0=BASELINE.equity0)
        print(f"    p_perdida={mc['p_perdida']:.1%} p_ruina={mc['p_ruina']:.1%} "
              f"p50 equity final={mc['percentiles_equity']['p50']:.0f}")
        out["monte_carlo"] = {k: v for k, v in mc.items() if k not in ("finals", "dds")}
    else:
        print(f"    only {len(base_trades)} trades — insufficient for Monte Carlo.")
        out["monte_carlo"] = None

    print("  6.5 Survivorship:")
    starts = [data[s].index[0] for s in PILOT_SYMBOLS]
    print(f"    all {len(PILOT_SYMBOLS)} pilot coins are listed since {max(starts)} "
          "or earlier — all veterans across the full sample window. Residual "
          "survivorship risk is in the PILOT SELECTION itself (BTC/ETH/SOL/AVAX/LINK "
          "chosen for CURRENT liquidity/market-cap), not in this sample's construction.")
    out["survivorship_note"] = (
        "All pilot coins pre-date the sample window; residual survivorship risk "
        "is in curating the pilot list by current liquidity, not in-sample listing bias."
    )

    section("7. Checklist verdict")
    checklist = {}
    checklist["wfo_beats_baseline_60pct"] = (
        out.get("fold_ttest", {}).get("win_rate", 0) > 0.6 if fold_rows else False)
    checklist["wfo_diff_significant_p05"] = (
        out.get("fold_ttest", {}).get("p", 1.0) < 0.05 if fold_rows else False)
    checklist["params_stable_gt50pct"] = (
        out.get("param_stability", {}).get("mode_count", 0) /
        max(1, out.get("param_stability", {}).get("n_folds", 1)) > 0.5 if fold_rows else False)
    checklist["dsr_gt_0_at_95"] = (out.get("dsr") or {}).get("dsr", 0) > 0.95
    checklist["pbo_lt_0.5"] = (out.get("pbo") or {}).get("pbo", 1.0) < 0.5
    checklist["survives_pessimistic_costs"] = bool(
        out.get("cost_sensitivity_attrs", {}).get("sobrevive_pesimista", False))
    checklist["top3_trades_lt_50pct"] = (
        out.get("pnl_concentration") or {}).get("top_3", 1.0) < 0.5 \
        if out.get("pnl_concentration") else False
    checklist["effective_bets_gte_8"] = (
        out.get("effective_bets") or {}).get("n_efectivo", 0) >= 8
    checklist["mc_ruin_prob_lt_20pct"] = (
        out.get("monte_carlo") or {}).get("p_ruina", 1.0) < 0.20 \
        if out.get("monte_carlo") else False
    checklist["lookahead_tests_pass"] = bool(
        out["lookahead_test"]["compute_n_pass"] and out["lookahead_test"]["channel_pass"])

    n_pass = sum(1 for v in checklist.values() if v)
    for k, v in checklist.items():
        print(f"  [{'x' if v else ' '}] {k}")
    print(f"\n  {n_pass}/{len(checklist)} criteria pass.")
    verdict = "NO DESPLIEGUES CAPITAL" if n_pass < len(checklist) * 0.7 else \
        "Condicionalmente viable — revisar criterios fallidos antes de asignar capital"
    print(f"  VEREDICTO: {verdict}")
    out["checklist"] = checklist
    out["n_criteria_pass"] = n_pass
    out["n_criteria_total"] = len(checklist)
    out["verdict"] = verdict

    with open("output/binance_perp_validation.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nResumen JSON escrito en output/binance_perp_validation.json")
    return out


if __name__ == "__main__":
    main()
