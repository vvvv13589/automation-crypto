"""Parameter search with an out-of-sample check.

Parameters are ranked on the first part of the data (in-sample) only; the
remaining part (out-of-sample) is reported so you can see whether the result
holds up on data the search never saw.
"""

from __future__ import annotations

import copy
import itertools
import logging
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from .backtest import run_backtest
from .strategy import AdaptiveStrategy

DEFAULT_GRID: dict[str, list] = {
    "strategy.stop_atr_mult": [1.5, 2.5, 3.5],
    "strategy.trail_atr_mult": [2.5, 3.5, 5.0],
    "strategy.adx_trend": [20, 25, 30],
    "strategy.trend_exit_on_di": [False, True],
}


def apply_overrides(cfg: dict, overrides: dict) -> dict:
    out = copy.deepcopy(cfg)
    for dotted, value in overrides.items():
        node = out
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return out


def split(candles: pd.DataFrame, cfg: dict, ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """In-sample / out-of-sample split; OOS keeps a warmup prefix for indicators."""
    warmup = AdaptiveStrategy(cfg["strategy"]).warmup
    cut = int(len(candles) * ratio)
    return candles.iloc[:cut], candles.iloc[max(0, cut - warmup):]


MIN_TRADES = 20


def robust(result: dict) -> bool:
    """Profitable on BOTH halves with enough trades to mean something."""
    a, b = result["in"], result["out"]
    return (a["total_return_pct"] > 0 and b["total_return_pct"] > 0
            and a["trades"] >= MIN_TRADES and b["trades"] >= MIN_TRADES // 2)


def score(summary: dict) -> float:
    """Return penalised by drawdown; too few trades is not trustworthy."""
    if summary["trades"] < 5:
        return float("-inf")
    return summary["total_return_pct"] + 0.5 * summary["max_drawdown_pct"]


def _evaluate(job):
    tf, overrides, cfg, ins, oos = job
    logging.getLogger("cryptobot").setLevel(logging.WARNING)
    run_cfg = apply_overrides(cfg, overrides)
    run_cfg["timeframe"] = tf
    # Judge the raw strategy: circuit breakers would cut every bad run short at
    # the same drawdown and make all combinations look alike.
    run_cfg["risk"]["max_drawdown"] = 1.0
    run_cfg["risk"]["daily_loss_limit"] = 1.0
    s_in = run_backtest(run_cfg, ins).summary()
    s_out = run_backtest(run_cfg, oos).summary()
    return {"timeframe": tf, "params": overrides, "in": s_in, "out": s_out, "score": score(s_in)}


def optimize(cfg: dict, candles_by_tf: dict[str, pd.DataFrame], grid: dict | None = None,
             ratio: float = 0.7, workers: int | None = None) -> list[dict]:
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    jobs = []
    for tf, candles in candles_by_tf.items():
        for values in itertools.product(*(grid[k] for k in keys)):
            overrides = dict(zip(keys, values))
            ins, oos = split(candles, apply_overrides(cfg, overrides), ratio)
            jobs.append((tf, overrides, cfg, ins, oos))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_evaluate, jobs, chunksize=4))
    return sorted(results, key=lambda r: r["score"], reverse=True)


def format_report(results: list[dict], top: int = 10) -> str:
    def short(params: dict) -> str:
        names = {"strategy.stop_atr_mult": "stop", "strategy.trail_atr_mult": "trail",
                 "strategy.adx_trend": "adx", "strategy.trend_exit_on_di": "di_exit"}
        return " ".join(f"{names.get(k, k.split('.')[-1])}={v}" for k, v in params.items())

    lines = [
        f"{'#':>2} {'TF':>4}  {'params':<38} | {'IS ret%':>8} {'IS DD%':>7} {'IS n':>5} {'IS B&H%':>8}"
        f" | {'OOS ret%':>8} {'OOS DD%':>7} {'OOS n':>5} {'OOS B&H%':>8}",
        "-" * 118,
    ]
    for i, r in enumerate(results[:top], 1):
        a, b = r["in"], r["out"]
        lines.append(
            f"{i:>2} {r['timeframe']:>4}  {short(r['params']):<38} | "
            f"{a['total_return_pct']:>8.2f} {a['max_drawdown_pct']:>7.2f} {a['trades']:>5} {a['buy_hold_return_pct']:>8.2f} | "
            f"{b['total_return_pct']:>8.2f} {b['max_drawdown_pct']:>7.2f} {b['trades']:>5} {b['buy_hold_return_pct']:>8.2f}"
        )
    return "\n".join(lines)


def best_yaml(result: dict) -> str:
    lines = [f"timeframe: {result['timeframe']}", "strategy:"]
    for k, v in result["params"].items():
        section, leaf = k.split(".", 1)
        if section == "strategy":
            lines.append(f"  {leaf}: {str(v).lower() if isinstance(v, bool) else v}")
    return "\n".join(lines)
