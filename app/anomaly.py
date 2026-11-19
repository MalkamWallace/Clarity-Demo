"""
Anomaly detection against history.

For each BU and line item, the outlook is compared to five years of history two ways:

1. Robust z-score of year-over-year growth (median / MAD), so one odd year
   in the history does not distort the baseline.
2. An Isolation Forest trained on every historical quarter for that line
   (all BUs pooled, each series standardized), using two features:
   YoY growth and the line's ratio to revenue versus the same quarter last year.
   The second feature catches cost lines that move out of step with revenue.

A line is flagged when the z-score is extreme on its own, or when the forest
calls it an outlier and the z-score agrees it is unusual.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from . import data_store as ds

Z_ALONE = 3.5
Z_WITH_FOREST = 2.0


def _features(values: pd.Series, revenue: pd.Series) -> pd.DataFrame:
    yoy = values / values.shift(4) - 1
    ratio = values / revenue
    ratio_chg = ratio - ratio.shift(4)
    return pd.DataFrame({"yoy": yoy, "ratio_chg": ratio_chg}).dropna()


def _robust_z(x: float, hist: np.ndarray) -> float:
    med = np.median(hist)
    mad = np.median(np.abs(hist - med)) * 1.4826
    mad = max(mad, 0.01)  # floor: 1 pt of growth, avoids divide-by-tiny on very stable lines
    return (x - med) / mad


def score_outlook(outlook: dict, quarter: str) -> list[dict]:
    """outlook: {(bu, line): value} for the quarter under review.

    Returns one record per (bu, line) with z-score, forest verdict and a flag.
    """
    h = ds.history()
    qk = ds.quarter_key(quarter)
    yoy_q = ds.key_to_quarter(qk - 4)
    results = []

    for line in ds.ALL_LINES:
        train, meta = [], {}
        for bu in ds.business_units():
            s = ds.series(bu, line)
            rev = ds.series(bu, "Revenue")
            s = s[[ds.quarter_key(q) < qk for q in s.index]]
            rev = rev.loc[s.index]
            f = _features(s, rev)
            if len(f) < 6:
                continue
            mu, sd = f.mean(), f.std().replace(0, 1e-6)
            train.append((f - mu) / sd)
            meta[bu] = (s, rev, f, mu, sd)
        if not train:
            continue
        X = pd.concat(train).values
        forest = IsolationForest(n_estimators=300, contamination=0.05, random_state=7).fit(X)

        for bu, (s, rev, f, mu, sd) in meta.items():
            cur = outlook.get((bu, line))
            cur_rev = outlook.get((bu, "Revenue"))
            if cur is None or yoy_q not in s.index or not cur_rev:
                continue
            base = s.loc[yoy_q]
            yoy = cur / base - 1 if base else 0.0
            ratio_chg = cur / cur_rev - base / rev.loc[yoy_q]
            z = _robust_z(yoy, f["yoy"].values)
            x = ((pd.Series({"yoy": yoy, "ratio_chg": ratio_chg}) - mu) / sd).values.reshape(1, -1)
            is_out = forest.predict(x)[0] == -1
            score = float(-forest.score_samples(x)[0])  # higher = more anomalous
            flagged = abs(z) >= Z_ALONE or (is_out and abs(z) >= Z_WITH_FOREST)
            lo, hi = np.percentile(f["yoy"].values, [5, 95])
            results.append(dict(
                bu=bu, line=line, value=cur, yoy=yoy, z=round(float(z), 2),
                forest_outlier=bool(is_out), anomaly_score=round(score, 3), flagged=bool(flagged),
                typical_low=float(lo), typical_high=float(hi),
            ))
    return results


def budget_variances(outlook: dict, plan_vals: dict, threshold: float) -> list[dict]:
    """Lines whose outlook misses plan by more than the guiding-principle threshold."""
    out = []
    for (bu, line), v in outlook.items():
        p = plan_vals.get((bu, line))
        if not p:
            continue
        pct = v / p - 1
        if abs(pct) < threshold:
            continue
        if line in ds.COST_LINES:
            direction = "over budget" if pct > 0 else "under budget"
        else:
            direction = "above plan" if pct > 0 else "below plan"
        out.append(dict(bu=bu, line=line, value=v, plan=p, pct=pct, direction=direction,
                        severity="high" if abs(pct) >= 2 * threshold else "medium"))
    return out
