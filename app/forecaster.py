"""
Forecast tool: P&L forecast up to 8 quarters from the FY22-FY26 catalogue.

For every BU and input line (Revenue, COGS, Opex, Units, Headcount):
  1. Hold out the last 4 quarters and fit five models on the rest.
  2. Score each model on the holdout (MAPE). This is the backtest.
  3. Model selection: if one model is clearly best (MAPE at least 20% lower than
     the runner-up), use it alone. Otherwise average the top three, weighted by
     1 / MAPE. This mirrors how the original tool chose between models.
  4. Refit the chosen model(s) on all history and forecast the horizon.
Gross Margin and Operating Income are never forecast directly. They are
calculated from the forecast lines, so the P&L always ties.

Ranges are 80% bands built from backtest error, widening with the horizon.
"""
from __future__ import annotations

import warnings
from functools import lru_cache

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from . import data_store as ds

warnings.filterwarnings("ignore")

MODELS = ["Holt-Winters", "Linear regression", "SARIMA", "Seasonal naive + drift", "Theta"]
HOLDOUT = 4
CLEAR_WINNER = 0.8
Z80 = 1.2816


# ------------------------------------------------------------------ the five models

def _holt_winters(y: np.ndarray, h: int) -> np.ndarray:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    m = ExponentialSmoothing(y, trend="add", seasonal="mul" if (y > 0).all() else "add",
                             seasonal_periods=4, damped_trend=True).fit(optimized=True)
    return m.forecast(h)


def _linear(y: np.ndarray, h: int) -> np.ndarray:
    n = len(y)
    t = np.arange(n + h)
    X = np.column_stack([t] + [(t % 4 == k).astype(float) for k in range(1, 4)])
    m = LinearRegression().fit(X[:n], y)
    return m.predict(X[n:])


def _sarima(y: np.ndarray, h: int) -> np.ndarray:
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    m = SARIMAX(y, order=(1, 0, 0), seasonal_order=(0, 1, 0, 4), trend="c",
                enforce_stationarity=False).fit(disp=False)
    return m.forecast(h)


def _snaive_drift(y: np.ndarray, h: int) -> np.ndarray:
    growth = np.median(y[4:] / y[:-4]) if len(y) > 4 else 1.0
    out, hist = [], list(y)
    for _ in range(h):
        nxt = hist[-4] * growth
        out.append(nxt)
        hist.append(nxt)
    return np.array(out)


def _theta(y: np.ndarray, h: int) -> np.ndarray:
    from statsmodels.tsa.forecasting.theta import ThetaModel
    m = ThetaModel(y, period=4, deseasonalize=True,
                   method="multiplicative" if (y > 0).all() else "additive").fit()
    return np.asarray(m.forecast(h))


FUNCS = {"Holt-Winters": _holt_winters, "Linear regression": _linear, "SARIMA": _sarima,
         "Seasonal naive + drift": _snaive_drift, "Theta": _theta}


def _run(name, y, h):
    try:
        f = np.asarray(FUNCS[name](y, h), dtype=float)
        if not np.isfinite(f).all():
            raise ValueError("non-finite")
        return f
    except Exception:  # noqa: BLE001  a model that fails to fit is skipped, not fatal
        return None


def _mape(actual, pred):
    return float(np.mean(np.abs((actual - pred) / actual)))


@lru_cache(maxsize=256)
def forecast_line(bu: str, line: str, horizon: int, hist_key: str = "catalogue",
                  values: tuple | None = None, labels: tuple | None = None) -> dict:
    if values is None:
        s = ds.series(bu, line)
        y, labels = s.values.astype(float), tuple(s.index)
    else:
        y = np.array(values, dtype=float)

    train, test = y[:-HOLDOUT], y[-HOLDOUT:]
    board = []
    for name in MODELS:
        p = _run(name, train, HOLDOUT)
        if p is not None:
            board.append(dict(model=name, mape=_mape(test, p), backtest=p.tolist()))
    board.sort(key=lambda b: b["mape"])
    if not board:
        raise RuntimeError(f"No model could fit {bu} {line}")

    if len(board) == 1 or board[0]["mape"] <= CLEAR_WINNER * board[1]["mape"]:
        chosen = [board[0]]
        method = f"{board[0]['model']} (clear winner in backtest)"
    else:
        chosen = board[:3]
        method = "Weighted average of " + ", ".join(b["model"] for b in chosen)
    w = np.array([1 / max(b["mape"], 1e-4) for b in chosen])
    w = w / w.sum()
    for b in board:
        b["weight"] = 0.0
    for b, wi in zip(chosen, w):
        b["weight"] = round(float(wi), 3)

    fits = [(_run(b["model"], y, horizon), wi) for b, wi in zip(chosen, w)]
    fits = [(f, wi) for f, wi in fits if f is not None]
    tw = sum(wi for _, wi in fits)
    fc = sum(f * wi for f, wi in fits) / tw

    bt = sum(np.array(b["backtest"]) * wi for b, wi in zip(chosen, w))
    rel_err = np.std((test - bt) / test, ddof=0) if len(test) > 1 else 0.03
    rel_err = max(float(rel_err), 0.01)
    steps = np.arange(1, horizon + 1)
    band = Z80 * rel_err * np.sqrt(steps) * np.abs(fc)

    last = ds.quarter_key(labels[-1])
    future = [ds.key_to_quarter(last + i) for i in range(1, horizon + 1)]
    return dict(bu=bu, line=line, history=dict(zip(labels, y.round(1).tolist())),
                forecast=dict(zip(future, fc.round(1).tolist())),
                low=dict(zip(future, (fc - band).round(1).tolist())),
                high=dict(zip(future, (fc + band).round(1).tolist())),
                method=method, backtest_mape=float(_mape(test, bt)),
                leaderboard=[{k: v for k, v in b.items() if k != "backtest"} for b in board],
                holdout=list(labels[-HOLDOUT:]), holdout_actual=test.round(1).tolist(),
                holdout_pred=np.round(bt, 1).tolist())


def run(bu: str = "Total", horizon: int = 4, principles_fy: str = "FY27",
        uploaded: pd.DataFrame | None = None) -> dict:
    horizon = int(max(1, min(8, horizon)))
    bus = ds.business_units() if uploaded is None else list(dict.fromkeys(uploaded["bu"]))
    targets = bus if bu == "Total" else [bu]
    if bu != "Total" and bu not in bus:
        raise ValueError(f"Unknown business unit {bu}")

    per_bu = {}
    for b in targets:
        per_bu[b] = {}
        for line in ds.INPUT_LINES:
            if uploaded is None:
                per_bu[b][line] = forecast_line(b, line, horizon)
            else:
                s = ds.series(b, line, uploaded)
                if len(s) < 12:
                    raise ValueError(f"{b} {line}: need at least 12 quarters of history, found {len(s)}")
                per_bu[b][line] = forecast_line(b, line, horizon, "upload", tuple(s.values), tuple(s.index))

    def combine(field, line):
        out = {}
        for b in targets:
            for q, v in per_bu[b][line][field].items():
                out[q] = out.get(q, 0.0) + v
        return {q: round(v, 1) for q, v in out.items()}

    pnl = {}
    for line in ds.INPUT_LINES:
        pnl[line] = {f: combine(f, line) for f in ("history", "forecast", "low", "high")}
    for f in ("history", "forecast"):
        pnl.setdefault("Gross Margin", {})[f] = {q: round(pnl["Revenue"][f][q] - pnl["COGS"][f][q], 1)
                                                 for q in pnl["Revenue"][f]}
        pnl.setdefault("Operating Income", {})[f] = {q: round(pnl["Gross Margin"][f][q] - pnl["Opex"][f][q], 1)
                                                     for q in pnl["Revenue"][f]}

    fq = list(pnl["Revenue"]["forecast"])
    hq = list(pnl["Revenue"]["history"])
    table = []
    for i, q in enumerate(fq):
        prev = fq[i - 1] if i else hq[-1]
        prev_src = "forecast" if i else "history"
        yoy_q = ds.key_to_quarter(ds.quarter_key(q) - 4)
        row = {"quarter": q}
        for line in ["Revenue", "COGS", "Gross Margin", "Opex", "Operating Income", "Units (K)", "Headcount"]:
            v = pnl[line]["forecast"][q]
            pv = pnl[line][prev_src][prev]
            yv = pnl[line]["forecast"].get(yoy_q, pnl[line]["history"].get(yoy_q))
            row[line] = round(v, 1)
            row[f"{line} QoQ"] = v / pv - 1 if pv else None
            row[f"{line} YoY"] = v / yv - 1 if yv else None
        row["GM %"] = pnl["Gross Margin"]["forecast"][q] / pnl["Revenue"]["forecast"][q]
        table.append(row)

    checks = _principle_checks(pnl, bu, principles_fy)
    models = [{"bu": b, "line": line, "method": per_bu[b][line]["method"],
               "backtest_mape": per_bu[b][line]["backtest_mape"],
               "leaderboard": per_bu[b][line]["leaderboard"]}
              for b in targets for line in ds.INPUT_LINES]
    acc = 1 - np.mean([m["backtest_mape"] for m in models if m["line"] in ("Revenue", "COGS", "Opex")])

    return {"bu": bu, "horizon": horizon, "principles_fy": principles_fy,
            "source": "uploaded file" if uploaded is not None else "FY22–FY26 catalogue",
            "series": {k: {f: v for f, v in d.items()} for k, d in pnl.items()},
            "table": table, "models": models, "principle_checks": checks,
            "backtest_accuracy": float(acc)}


def _principle_checks(pnl, bu, fy) -> list[dict]:
    """Compare the forecast fiscal year to that year's guiding principles."""
    gp = ds.principles()
    row = gp[(gp.fiscal_year == fy) & (gp.bu == bu)]
    if row.empty:
        return []
    row = row.iloc[0]
    fy_num = int(fy[2:])
    in_fy = lambda d: {q: v for q, v in d.items() if ds.quarter_key(q) // 4 == fy_num}  # noqa: E731
    prev_fy = lambda d: {q: v for q, v in d.items() if ds.quarter_key(q) // 4 == fy_num - 1}  # noqa: E731

    def year_total(line, which):
        both = {**pnl[line]["history"], **pnl[line]["forecast"]}
        return sum(which(both).values()), len(which(both))

    rev, n = year_total("Revenue", in_fy)
    if n < 4:
        return [{"metric": "Coverage", "status": "info",
                 "detail": f"Forecast covers {n} of 4 quarters in {fy}. Extend the horizon to test the full year."}]
    rev_p, _ = year_total("Revenue", prev_fy)
    gm, _ = year_total("Gross Margin", in_fy)
    opex, _ = year_total("Opex", in_fy)
    out = []

    def item(metric, actual, target, higher_is_good, tol):
        gap = actual - target
        ok = gap >= -tol if higher_is_good else gap <= tol
        out.append(dict(metric=metric, forecast=actual, target=target, gap=gap,
                        status="on track" if ok else "at risk"))

    if pd.notna(row.revenue_growth_target) and rev_p:
        item("Revenue growth", rev / rev_p - 1, float(row.revenue_growth_target), True, 0.005)
    item("Gross margin %", gm / rev, float(row.gross_margin_pct_target), True, 0.005)
    item("Opex % of revenue", opex / rev, float(row.opex_pct_of_revenue_target), False, 0.005)
    return out
