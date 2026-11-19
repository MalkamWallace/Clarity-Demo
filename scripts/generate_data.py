"""
Generate all synthetic data for Clarity Demo.

Everything here is invented. "Meridian Systems" is a fictional company with
three business units. No real company figures are used.

Outputs
  data/history.csv          actuals FY22 Q1 to FY26 Q4, long format
  data/plan.csv             quarterly budget (plan) FY22 Q1 to FY27 Q1
  data/guiding_principles.csv  annual budget targets per BU, FY22 to FY27
  samples/control_book_FY27Q1_clean.xlsx
  samples/control_book_FY27Q1_with_issues.xlsx
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SAMPLES = ROOT / "samples"

RNG = np.random.default_rng(27)

FYS = [22, 23, 24, 25, 26]
QUARTERS = [f"FY{fy} Q{q}" for fy in FYS for q in range(1, 5)]
NEXT_Q = "FY27 Q1"

# Business unit profiles (values in $M per quarter, units in K, headcount in people)
BUS = {
    "Devices": dict(rev=900, rev_growth=0.035, season=[0.95, 0.98, 1.00, 1.12], gm=0.22,
                    opex_ratio=0.10, asp=0.20, hc=4200),
    "Infrastructure": dict(rev=600, rev_growth=0.06, season=[0.94, 1.00, 0.99, 1.10], gm=0.32,
                           opex_ratio=0.13, asp=5.0, hc=3100),
    "Software & Services": dict(rev=220, rev_growth=0.14, season=[0.97, 0.99, 1.00, 1.06], gm=0.70,
                                opex_ratio=0.30, asp=0.55, hc=1900),
}

INPUT_LINES = ["Revenue", "COGS", "Opex", "Units (K)", "Headcount"]
ALL_LINES = ["Revenue", "COGS", "Gross Margin", "Opex", "Operating Income", "Units (K)", "Headcount"]


def expected(bu: str, i: int) -> dict:
    """Noise-free 'true' value for quarter index i (0 = FY22 Q1)."""
    p = BUS[bu]
    yrs = i / 4
    q = i % 4
    rev = p["rev"] * (1 + p["rev_growth"]) ** yrs * p["season"][q]
    gm_drift = 0.004 * yrs  # slow margin improvement
    cogs = rev * (1 - p["gm"] - gm_drift)
    opex = rev * p["opex_ratio"] * (1 - 0.01 * yrs) + 8
    units = rev / p["asp"]
    hc = p["hc"] * (1 + 0.02 * yrs)
    return {"Revenue": rev, "COGS": cogs, "Opex": opex, "Units (K)": units, "Headcount": hc}


def noisy(vals: dict) -> dict:
    out = {}
    for k, v in vals.items():
        sd = 0.006 if k == "Headcount" else 0.025
        out[k] = v * (1 + RNG.normal(0, sd))
    return out


def derive(vals: dict) -> dict:
    v = dict(vals)
    v["Gross Margin"] = v["Revenue"] - v["COGS"]
    v["Operating Income"] = v["Gross Margin"] - v["Opex"]
    return v


def rnd(line: str, x: float) -> float:
    return round(x) if line == "Headcount" else round(x, 1)


def build_history_and_plan():
    hist_rows, plan_rows = [], []
    for bu in BUS:
        for i, label in enumerate(QUARTERS + [NEXT_Q]):
            exp = expected(bu, i)
            # Plan: built from the expected path with a small stretch target
            plan = {k: v * (1.015 if k in ("Revenue", "COGS", "Units (K)") else 1.0) for k, v in exp.items()}
            plan = derive(plan)
            for line in ALL_LINES:
                plan_rows.append(dict(quarter=label, bu=bu, line=line, value=rnd(line, plan[line])))
            if label == NEXT_Q:
                continue
            act = derive(noisy(exp))
            for line in ALL_LINES:
                hist_rows.append(dict(quarter=label, bu=bu, line=line, value=rnd(line, act[line])))
    # Re-derive rounded derived lines so everything ties exactly
    hist = _long(hist_rows, True)
    plan = _long(plan_rows, True)
    return hist, plan


def _long(rows, retie: bool) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    w = df.pivot_table(index=["quarter", "bu"], columns="line", values="value").reset_index()
    if retie:
        w["Gross Margin"] = (w["Revenue"] - w["COGS"]).round(1)
        w["Operating Income"] = (w["Gross Margin"] - w["Opex"]).round(1)
    long = w.melt(id_vars=["quarter", "bu"], var_name="line", value_name="value")
    order = {q: i for i, q in enumerate(QUARTERS + [NEXT_Q])}
    long["_q"] = long["quarter"].map(order)
    long["_b"] = long["bu"].map({b: i for i, b in enumerate(BUS)})
    long["_l"] = long["line"].map({l: i for i, l in enumerate(ALL_LINES)})
    return long.sort_values(["_q", "_b", "_l"]).drop(columns=["_q", "_b", "_l"]).reset_index(drop=True)


def build_guiding_principles(plan: pd.DataFrame) -> pd.DataFrame:
    """Annual targets derived from each year's budget, which is how the original tool worked."""
    rows = []
    p = plan.copy()
    p["fy"] = p["quarter"].str.slice(0, 4)
    for fy in sorted(p["fy"].unique()):
        prev = f"FY{int(fy[2:]) - 1}"
        for bu in list(BUS) + ["Total"]:
            cur = p[p.fy == fy] if bu == "Total" else p[(p.fy == fy) & (p.bu == bu)]
            pre = p[p.fy == prev] if bu == "Total" else p[(p.fy == prev) & (p.bu == bu)]
            if fy == "FY27":
                # Only Q1 of FY27 is planned in quarterly detail; annual targets set explicitly
                pass
            s = cur.groupby("line")["value"].sum()
            rev_growth = None
            if len(pre) and fy != "FY27":
                ps = pre.groupby("line")["value"].sum()
                rev_growth = s["Revenue"] / ps["Revenue"] - 1
            gm_pct = s["Gross Margin"] / s["Revenue"]
            opex_pct = s["Opex"] / s["Revenue"]
            rows.append(dict(fiscal_year=fy, bu=bu,
                             revenue_growth_target=None if rev_growth is None else round(rev_growth, 3),
                             gross_margin_pct_target=round(gm_pct, 3),
                             opex_pct_of_revenue_target=round(opex_pct, 3),
                             max_variance_to_plan_pct=0.10))
    gp = pd.DataFrame(rows)
    # Explicit FY27 growth targets (the budget letter for next year)
    fy27 = {"Devices": 0.04, "Infrastructure": 0.07, "Software & Services": 0.15, "Total": 0.065}
    for bu, g in fy27.items():
        gp.loc[(gp.fiscal_year == "FY27") & (gp.bu == bu), "revenue_growth_target"] = g
    # FY22 has no prior year in the data; use stated targets
    fy22 = {"Devices": 0.03, "Infrastructure": 0.05, "Software & Services": 0.12, "Total": 0.05}
    for bu, g in fy22.items():
        gp.loc[(gp.fiscal_year == "FY22") & (gp.bu == bu), "revenue_growth_target"] = g
    return gp


# ---------------------------------------------------------------- control book

NAVY = "0B2D5B"
BLUE = "0672CB"
LIGHT = "E8F1FB"
thin = Side(style="thin", color="C9D3E0")

COLS = ["BU", "Line Item", "FY26 Q1 Actual", "FY26 Q4 Actual", "FY27 Q1 Plan", "FY27 Q1 Outlook",
        "QoQ $", "QoQ %", "YoY $", "YoY %", "vs Plan $", "vs Plan %"]
HEADER_ROW = 6
FIRST_ROW = 7


def build_control_book(hist: pd.DataFrame, plan: pd.DataFrame, outlook: dict, issues: bool) -> Workbook:
    """outlook: {(bu, input_line): value} for FY27 Q1."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Control Book"
    ws["A1"] = "Meridian Systems  |  Spend & P&L Control Book"
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A2"] = "FY27 Q1 Outlook review  |  $M unless noted  |  Fictional data for demo purposes"
    ws["A2"].font = Font(italic=True, color="5A6B7F")
    ws["A3"] = "Version: " + ("Draft v3 (pre-review)" if issues else "Final v4")
    ws["A3"].font = Font(color="5A6B7F")

    for j, h in enumerate(COLS, start=1):
        c = ws.cell(row=HEADER_ROW, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=BLUE)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[HEADER_ROW].height = 30

    def hv(q, bu, line):
        return float(hist[(hist.quarter == q) & (hist.bu == bu) & (hist.line == line)]["value"].iloc[0])

    def pv(bu, line):
        return float(plan[(plan.quarter == NEXT_Q) & (plan.bu == bu) & (plan.line == line)]["value"].iloc[0])

    row = FIRST_ROW
    rows_of = {}  # (bu, line) -> row
    for bu in BUS:
        for line in ALL_LINES:
            rows_of[(bu, line)] = row
            ws.cell(row=row, column=1, value=bu)
            ws.cell(row=row, column=2, value=line)
            row += 1
        row += 1  # spacer
    total_start = row
    for line in ALL_LINES:
        rows_of[("Total", line)] = row
        ws.cell(row=row, column=1, value="Total")
        ws.cell(row=row, column=2, value=line)
        row += 1
    last_row = row - 1

    src_cols = {"C": ("hist", "FY26 Q1"), "D": ("hist", "FY26 Q4"), "E": ("plan", None), "F": ("outlook", None)}

    for (bu, line), r in rows_of.items():
        for col, (kind, q) in src_cols.items():
            cell = f"{col}{r}"
            if bu == "Total":
                refs = ",".join(f"{col}{rows_of[(b, line)]}" for b in BUS)
                ws[cell] = f"=SUM({refs})"
            elif line == "Gross Margin":
                ws[cell] = f"={col}{rows_of[(bu, 'Revenue')]}-{col}{rows_of[(bu, 'COGS')]}"
            elif line == "Operating Income":
                ws[cell] = f"={col}{rows_of[(bu, 'Gross Margin')]}-{col}{rows_of[(bu, 'Opex')]}"
            else:
                if kind == "hist":
                    v = hv(q, bu, line)
                elif kind == "plan":
                    v = pv(bu, line)
                else:
                    v = outlook[(bu, line)]
                ws[cell] = v
        ws[f"G{r}"] = f"=F{r}-D{r}"
        ws[f"H{r}"] = f"=IFERROR(G{r}/D{r},0)"
        ws[f"I{r}"] = f"=F{r}-C{r}"
        ws[f"J{r}"] = f"=IFERROR(I{r}/C{r},0)"
        ws[f"K{r}"] = f"=F{r}-E{r}"
        ws[f"L{r}"] = f"=IFERROR(K{r}/E{r},0)"

    if issues:
        # 1. Hardcoded Gross Margin (stale paste from an earlier version)
        r = rows_of[("Infrastructure", "Gross Margin")]
        ws[f"F{r}"] = 241.6
        # 2. QoQ % references the wrong row
        r = rows_of[("Devices", "Opex")]
        ws[f"H{r}"] = f"=IFERROR(G{r}/D{r - 1},0)"
        # 3. Total Revenue outlook drops Software & Services from the sum
        r = rows_of[("Total", "Revenue")]
        ws[f"F{r}"] = f"=SUM(F{rows_of[('Devices', 'Revenue')]},F{rows_of[('Infrastructure', 'Revenue')]})"
        # 4. Total Opex outlook counts Devices twice (repeated calculation)
        r = rows_of[("Total", "Opex")]
        d = rows_of[("Devices", "Opex")]
        ws[f"F{r}"] = (f"=SUM(F{d},F{rows_of[('Infrastructure', 'Opex')]},"
                       f"F{rows_of[('Software & Services', 'Opex')]},F{d})")
        # 5. Broken reference
        r = rows_of[("Infrastructure", "Units (K)")]
        ws[f"L{r}"] = f"=K{r}/#REF!"

    # Formatting
    for r in range(FIRST_ROW, last_row + 1):
        line = ws.cell(row=r, column=2).value
        bu = ws.cell(row=r, column=1).value
        if line is None:
            continue
        for j in range(1, 13):
            c = ws.cell(row=r, column=j)
            c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
            if j >= 3:
                if j in (8, 10, 12):
                    c.number_format = "0.0%"
                elif line == "Headcount":
                    c.number_format = "#,##0"
                else:
                    c.number_format = "#,##0.0"
            if bu == "Total":
                c.font = Font(bold=True)
                c.fill = PatternFill("solid", fgColor=LIGHT)
            if line in ("Gross Margin", "Operating Income") and j == 2:
                c.font = Font(bold=True)
    widths = [22, 18, 13, 13, 13, 14, 11, 9, 11, 9, 11, 10]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + j)].width = w
    ws.freeze_panes = "C7"
    return wb


def make_outlook(plan: pd.DataFrame, issues: bool) -> dict:
    out = {}
    for bu in BUS:
        exp = expected(bu, len(QUARTERS))
        vals = noisy(exp)
        for line in INPUT_LINES:
            out[(bu, line)] = rnd(line, vals[line])
    if issues:
        # Anomalies the tool should catch
        base = out[("Software & Services", "COGS")]
        out[("Software & Services", "COGS")] = rnd("COGS", base * 1.62)   # COGS spike far above history
        out[("Infrastructure", "Units (K)")] = rnd("Units (K)", out[("Infrastructure", "Units (K)")] * 0.68)
        out[("Devices", "Opex")] = rnd("Opex", out[("Devices", "Opex")] * 1.21)  # over budget
    return out


def recalc_with_libreoffice(path: Path):
    """Open and re-save through LibreOffice so the file carries cached values like an Excel save."""
    if not shutil.which("soffice"):
        print("  soffice not found; file saved without cached values")
        return
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["soffice", "--headless", "--calc", "--convert-to", "xlsx:Calc MS Excel 2007 XML",
                        "--outdir", td, str(path)], check=True, capture_output=True, timeout=120)
        shutil.copy(Path(td) / path.name, path)


def main():
    DATA.mkdir(exist_ok=True)
    SAMPLES.mkdir(exist_ok=True)
    hist, plan = build_history_and_plan()
    gp = build_guiding_principles(plan)
    hist.to_csv(DATA / "history.csv", index=False)
    plan.to_csv(DATA / "plan.csv", index=False)
    gp.to_csv(DATA / "guiding_principles.csv", index=False)
    print(f"history rows {len(hist)}, plan rows {len(plan)}, principles rows {len(gp)}")

    state = RNG.bit_generator.state
    clean_outlook = make_outlook(plan, issues=False)
    RNG.bit_generator.state = state
    issue_outlook = make_outlook(plan, issues=True)

    for name, outlook, issues in [("control_book_FY27Q1_clean.xlsx", clean_outlook, False),
                                  ("control_book_FY27Q1_with_issues.xlsx", issue_outlook, True)]:
        p = SAMPLES / name
        build_control_book(hist, plan, outlook, issues).save(p)
        recalc_with_libreoffice(p)
        print("wrote", p.relative_to(ROOT))


if __name__ == "__main__":
    main()
