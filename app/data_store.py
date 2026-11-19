"""Loads the 'backend' catalogue: historical actuals, quarterly plan, guiding principles."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SAMPLES = ROOT / "samples"

DOLLAR_LINES = ["Revenue", "COGS", "Gross Margin", "Opex", "Operating Income"]
INPUT_LINES = ["Revenue", "COGS", "Opex", "Units (K)", "Headcount"]
ALL_LINES = ["Revenue", "COGS", "Gross Margin", "Opex", "Operating Income", "Units (K)", "Headcount"]
COST_LINES = {"COGS", "Opex"}

SAMPLE_FILES = {
    "with_issues": ("control_book_FY27Q1_with_issues.xlsx", "FY27 Q1 Outlook, Draft v3 (has planted issues)"),
    "clean": ("control_book_FY27Q1_clean.xlsx", "FY27 Q1 Outlook, Final v4 (clean)"),
}


def quarter_key(q: str) -> int:
    """'FY26 Q3' -> sortable integer."""
    fy, qq = q.split()
    return int(fy[2:]) * 4 + int(qq[1:]) - 1


def key_to_quarter(k: int) -> str:
    return f"FY{k // 4:02d} Q{k % 4 + 1}"


@lru_cache
def history() -> pd.DataFrame:
    df = pd.read_csv(DATA / "history.csv")
    df["qkey"] = df["quarter"].map(quarter_key)
    return df


@lru_cache
def plan() -> pd.DataFrame:
    df = pd.read_csv(DATA / "plan.csv")
    df["qkey"] = df["quarter"].map(quarter_key)
    return df


@lru_cache
def principles() -> pd.DataFrame:
    return pd.read_csv(DATA / "guiding_principles.csv")


def business_units() -> list[str]:
    return list(dict.fromkeys(history()["bu"]))


def series(bu: str, line: str, df: pd.DataFrame | None = None) -> pd.Series:
    """Quarterly series for one BU and line, indexed by quarter label, sorted. bu='Total' sums BUs."""
    df = history() if df is None else df
    d = df[df.line == line] if bu == "Total" else df[(df.bu == bu) & (df.line == line)]
    s = d.groupby("quarter")["value"].sum()
    return s.loc[sorted(s.index, key=quarter_key)]


def catalogue() -> dict:
    h = history()
    gp = principles()
    qs = sorted(h["quarter"].unique(), key=quarter_key)
    return {
        "company": "Meridian Systems (fictional)",
        "history_range": [qs[0], qs[-1]],
        "quarters": len(qs),
        "business_units": business_units(),
        "lines": ALL_LINES,
        "guiding_principles": sorted(gp["fiscal_year"].unique()),
        "samples": {k: v[1] for k, v in SAMPLE_FILES.items()},
    }
