"""
Check tool: accuracy audit, anomaly detection, key movers and commentary for one control book.

Pipeline
  1. Read the workbook twice: once for formulas, once for the values Excel last displayed.
  2. Recompute every formula with our own evaluator.
  3. Run formula checks: broken references, inconsistent formulas, hardcoded values,
     repeated references, stale displayed values.
  4. Run tie-outs: Gross Margin = Revenue - COGS, Operating Income = GM - Opex,
     Total = sum of business units.
  5. Score the outlook against FY22-FY26 history (anomaly.py) and against plan.
  6. Rank key movers on a blend of $ and % change and draft commentary.
"""
from __future__ import annotations

import io
import re
from collections import Counter, defaultdict

from openpyxl import load_workbook

from . import anomaly, commentary
from . import data_store as ds
from .formula_eval import (Evaluator, FormulaError, RefError, has_error_literal, references,
                           relative_signature, tokenize)

TOL = 0.15  # $M tolerance for tie-outs (rounding)
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

VALUE_ROLES = ["yoy_base", "qoq_base", "plan", "outlook"]
METRIC_HEADERS = {"qoq $": "qoq_d", "qoq %": "qoq_p", "yoy $": "yoy_d", "yoy %": "yoy_p",
                  "vs plan $": "plan_d", "vs plan %": "plan_p"}
QUARTER_RE = re.compile(r"FY\d{2}\s*Q[1-4]", re.I)


class ControlBookError(ValueError):
    pass


# ------------------------------------------------------------------ reading the book

def _locate(ws):
    for row in ws.iter_rows(min_row=1, max_row=40):
        for c in row:
            if isinstance(c.value, str) and c.value.strip().lower() == "line item":
                return c.row, c.column
    raise ControlBookError("Could not find a 'Line Item' header. Is this in the control book format?")


def _map_columns(ws, header_row):
    roles, metrics, labels, actual_cols = {}, {}, {}, []
    for c in ws[header_row]:
        h = str(c.value or "").strip()
        hl = h.lower()
        if hl in METRIC_HEADERS:
            metrics[METRIC_HEADERS[hl]] = c.column_letter
        elif "actual" in hl:
            actual_cols.append((c.column_letter, h))
        elif "plan" in hl and "vs" not in hl:
            roles["plan"] = c.column_letter
            labels["plan"] = h
        elif "outlook" in hl or "forecast" in hl:
            roles["outlook"] = c.column_letter
            labels["outlook"] = h
    if len(actual_cols) >= 2:
        (roles["yoy_base"], labels["yoy_base"]), (roles["qoq_base"], labels["qoq_base"]) = actual_cols[:2]
    missing = [r for r in VALUE_ROLES if r not in roles]
    if missing:
        raise ControlBookError(f"Missing columns: {', '.join(missing)}. Expected two Actual columns, "
                               "a Plan column and an Outlook column.")
    quarters = {}
    for r, lab in labels.items():
        m = QUARTER_RE.search(lab)
        quarters[r] = re.sub(r"\s+", " ", m.group(0).upper()) if m else None
    return roles, metrics, labels, quarters


def analyze(file_bytes: bytes, basis: str = "yoy", principles_fy: str | None = None,
            top_n: int = 10, filename: str = "") -> dict:
    try:
        wb_f = load_workbook(io.BytesIO(file_bytes), data_only=False)
        wb_v = load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ControlBookError(f"Could not open the file as an Excel workbook ({type(e).__name__}).")
    ws = wb_f.worksheets[0]
    wsv = wb_v.worksheets[0]
    header_row, line_col = _locate(ws)
    bu_col = line_col - 1
    roles, metrics, labels, quarters = _map_columns(ws, header_row)

    # rows
    rows = []  # (row, bu, line)
    seen = Counter()
    for r in range(header_row + 1, ws.max_row + 1):
        bu = ws.cell(row=r, column=bu_col).value if bu_col >= 1 else None
        line = ws.cell(row=r, column=line_col).value
        if not line:
            continue
        rows.append((r, str(bu or "").strip(), str(line).strip()))
        seen[(str(bu or "").strip(), str(line).strip())] += 1
    if not rows:
        raise ControlBookError("No line items found under the header row.")

    raw = {}
    formulas = {}
    cached = {}
    for row in ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row):
        for c in row:
            if c.value is None:
                continue
            ref = c.coordinate
            if isinstance(c.value, str) and c.value.startswith("="):
                formulas[ref] = c.value
            else:
                raw[ref] = c.value
            cached[ref] = wsv[ref].value

    # recompute everything
    memo, errors = {}, {}

    def get(cell, stack=()):
        if cell in memo:
            return memo[cell]
        if cell in formulas:
            if cell in stack:
                raise FormulaError("circular reference")
            ev = Evaluator(lambda c: get(c, stack + (cell,)))
            try:
                v = ev.eval_formula(formulas[cell])
            except RefError as e:
                errors[cell] = str(e)
                v = f"{e}" if str(e).startswith("#") else "#ERR"
            except FormulaError as e:
                errors[cell] = str(e)
                v = "#ERR"
            memo[cell] = v
            return v
        v = raw.get(cell)
        memo[cell] = v
        return v

    for cell in formulas:
        get(cell)

    def val(col, r):
        v = get(f"{col}{r}")
        return float(v) if isinstance(v, (int, float)) else None

    findings = defaultdict(list)  # cell -> list of findings

    def add(cell, severity, category, title, detail, r=None):
        r = r or int(re.sub(r"\D", "", cell))
        bu, line = next(((b, l) for rr, b, l in rows if rr == r), ("", ""))
        findings[cell].append(dict(cell=cell, severity=severity, category=category, title=title,
                                   detail=detail, bu=bu, line=line))

    # --- 1. broken formulas
    for cell, f in formulas.items():
        try:
            tokenize(f)
        except FormulaError as e:
            add(cell, "high", "Broken formula", "Formula could not be read", f"{f}  ({e})")
            continue
        if has_error_literal(f):
            add(cell, "high", "Broken formula", "Formula points to a deleted reference",
                f"{f} contains #REF!. A row or column it depended on was removed.")
        elif cell in errors:
            add(cell, "high", "Broken formula", "Formula returns an error", f"{f} → {errors[cell]}")

    # --- 2. repeated references (double counting)
    for cell, f in formulas.items():
        try:
            refs = references(f)
        except FormulaError:
            continue
        dup = [r for r, n in Counter(refs).items() if n > 1]
        if dup:
            add(cell, "high", "Repeated calculation", "Same cell counted more than once",
                f"{f} includes {', '.join(dup)} twice, which double counts it.")
    for (bu, line), n in seen.items():
        if n > 1 and line:
            r = next(rr for rr, b, l in rows if (b, l) == (bu, line))
            add(f"{ws.cell(row=r, column=line_col).column_letter}{r}", "medium", "Repeated calculation",
                "Line item appears more than once", f"{bu} / {line} appears {n} times.", r)

    # --- 3. inconsistent formulas and hardcodes, by peer group
    value_cols = [roles[k] for k in VALUE_ROLES]
    groups = []
    for _, col in metrics.items():  # each metric column is one peer group
        groups.append([f"{col}{r}" for r, _, _ in rows])
    for r, bu, line in rows:  # each derived row across value columns is one peer group
        cells = [f"{c}{r}" for c in value_cols]
        if sum(c in formulas for c in cells) >= 2:
            groups.append(cells)

    for cells in groups:
        sigs = {}
        for c in cells:
            if c in formulas:
                try:
                    sigs[c] = relative_signature(formulas[c], c)
                except FormulaError:
                    pass
        if len(sigs) < 2:
            continue
        common, n = Counter(sigs.values()).most_common(1)[0]
        if n < 2:
            continue
        example = next(c for c, s in sigs.items() if s == common)
        for c, s in sigs.items():
            if s != common and not any(f["category"] in ("Repeated calculation", "Broken formula")
                                       for f in findings.get(c, [])):
                add(c, "high", "Inconsistent formula", "Formula differs from its neighbors",
                    f"{formulas[c]} does not follow the pattern used in {example} ({formulas[example]}). "
                    "It may point at the wrong row or leave something out.")
        for c in cells:
            if c not in formulas and isinstance(raw.get(c), (int, float)) and len(sigs) >= 2:
                add(c, "high", "Hardcoded value", "Typed number where a formula belongs",
                    f"{c} holds a typed value ({raw[c]:,.1f}) while {example} and its neighbors calculate. "
                    "Typed values go stale when inputs change.")

    # --- 4. tie-outs on the recomputed values
    idx = {(bu, line): r for r, bu, line in rows}
    bus = [b for b in dict.fromkeys(b for _, b, _ in rows) if b and b.lower() != "total"]
    for col_role in VALUE_ROLES:
        col = roles[col_role]
        for bu in bus + ["Total"]:
            def v(line):
                r = idx.get((bu, line))
                return (val(col, r), r) if r else (None, None)
            (rev, _), (cogs, _), (gm, rgm) = v("Revenue"), v("COGS"), v("Gross Margin")
            (opex, _), (oi, roi) = v("Opex"), v("Operating Income")
            if None not in (rev, cogs, gm) and abs(gm - (rev - cogs)) > TOL:
                add(f"{col}{rgm}", "high", "Does not tie", "Gross Margin ≠ Revenue − COGS",
                    f"Revenue {rev:,.1f} − COGS {cogs:,.1f} = {rev - cogs:,.1f}, but the cell shows "
                    f"{gm:,.1f} (off by {gm - (rev - cogs):+,.1f}).")
            if None not in (gm, opex, oi) and abs(oi - (gm - opex)) > TOL:
                add(f"{col}{roi}", "high", "Does not tie", "Operating Income ≠ Gross Margin − Opex",
                    f"GM {gm:,.1f} − Opex {opex:,.1f} = {gm - opex:,.1f}, but the cell shows {oi:,.1f}.")
        for line in ds.ALL_LINES:
            rt = idx.get(("Total", line))
            if not rt:
                continue
            parts = [val(col, idx[(b, line)]) for b in bus if (b, line) in idx]
            tot = val(col, rt)
            if tot is not None and None not in parts and abs(tot - sum(parts)) > TOL:
                add(f"{col}{rt}", "high", "Does not tie", f"Total {line} ≠ sum of business units",
                    f"BUs sum to {sum(parts):,.1f}, total shows {tot:,.1f} (off by {tot - sum(parts):+,.1f}).")

    # --- 5. displayed values that don't match their formula (stale / not recalculated)
    for cell in formulas:
        cv, rv = cached.get(cell), memo.get(cell)
        if isinstance(cv, (int, float)) and isinstance(rv, (int, float)):
            tol = 0.0005 if abs(rv) < 5 else TOL
            if abs(cv - rv) > tol:
                add(cell, "medium", "Stale value", "Displayed number doesn't match its formula",
                    f"Shows {cv:,.3f}, recalculates to {rv:,.3f}. Recalculate before sending.")

    # --- 6. history and plan
    outlook_q = quarters.get("outlook")
    outlook = {(bu, line): val(roles["outlook"], r) for (bu, line), r in idx.items()
               if bu in bus and val(roles["outlook"], r) is not None}
    plan_vals = {(bu, line): val(roles["plan"], r) for (bu, line), r in idx.items()
                 if bu in bus and val(roles["plan"], r) is not None}

    gp = ds.principles()
    fy = principles_fy or (outlook_q.split()[0] if outlook_q else gp["fiscal_year"].max())
    gp_row = gp[(gp.fiscal_year == fy) & (gp.bu == "Total")]
    threshold = float(gp_row["max_variance_to_plan_pct"].iloc[0]) if len(gp_row) else 0.10

    anomalies = []
    history_note = None
    known_q = set(ds.history()["quarter"])
    if outlook_q and ds.key_to_quarter(ds.quarter_key(outlook_q) - 4) in known_q and set(bus) <= set(ds.business_units()):
        anomalies = anomaly.score_outlook(outlook, outlook_q)
        for a in anomalies:
            if a["flagged"]:
                r = idx[(a["bu"], a["line"])]
                add(f"{roles['outlook']}{r}", "high" if abs(a["z"]) >= Z_HIGH else "medium",
                    "Anomaly vs history", f"{a['line']} growth far outside its 5-year pattern",
                    f"{a['yoy'] * 100:+.1f}% YoY vs a typical {a['typical_low'] * 100:+.0f}% to "
                    f"{a['typical_high'] * 100:+.0f}% (z = {a['z']:+.1f}"
                    f"{', isolation forest outlier' if a['forest_outlier'] else ''}).")
    else:
        history_note = ("History comparison skipped: the outlook quarter or business units are not in the "
                        "FY22–FY26 catalogue.")

    budget = anomaly.budget_variances(outlook, plan_vals, threshold)
    for b in budget:
        r = idx[(b["bu"], b["line"])]
        add(f"{roles['outlook']}{r}", b["severity"], "Budget variance", f"{b['line']} {b['direction']}",
            f"Outlook {b['value']:,.1f} vs plan {b['plan']:,.1f} ({b['pct'] * 100:+.1f}%), "
            f"beyond the ±{threshold * 100:.0f}% guiding-principle threshold.")

    # --- 7. key movers (computed from recomputed inputs, not the sheet's own % columns)
    base_role = {"yoy": "yoy_base", "qoq": "qoq_base", "plan": "plan"}[basis]
    anomalous = {(a["bu"], a["line"]) for a in anomalies if a["flagged"]}
    cands = []
    for (bu, line), r in idx.items():
        if bu not in bus or line not in ds.DOLLAR_LINES:
            continue
        cur, base, pl = val(roles["outlook"], r), val(roles[base_role], r), val(roles["plan"], r)
        if cur is None or not base:
            continue
        cands.append(dict(bu=bu, line=line, value=cur, base=base, change=cur - base,
                          pct=(cur - base) / abs(base),
                          vs_plan=None if pl is None else cur - pl,
                          vs_plan_pct=None if not pl else (cur - pl) / abs(pl),
                          anomaly=(bu, line) in anomalous))
    if cands:
        def ranks(key):
            order = sorted(cands, key=lambda m: abs(m[key]))
            return {id(m): i / max(len(order) - 1, 1) for i, m in enumerate(order)}
        rd, rp = ranks("change"), ranks("pct")
        for m in cands:
            m["score"] = round(0.5 * rd[id(m)] + 0.5 * rp[id(m)], 3)
        cands.sort(key=lambda m: m["score"], reverse=True)
    movers = cands[:top_n]
    context = {f"{m['bu']} {m['line']}": round(m["pct"], 3) for m in cands}
    comm = commentary.build(movers, basis, context)

    # --- summary
    issue_list = []
    for cell, fs in findings.items():
        fs.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
        top = fs[0]
        issue_list.append(dict(cell=cell, bu=top["bu"], line=top["line"], severity=top["severity"],
                               categories=list(dict.fromkeys(f["category"] for f in fs)),
                               title=top["title"], details=[f["detail"] for f in fs]))
    issue_list.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], _cell_sort(i["cell"])))

    formula_cells = len(formulas)
    bad_formula_cells = {i["cell"] for i in issue_list
                         if set(i["categories"]) & {"Broken formula", "Inconsistent formula",
                                                    "Repeated calculation", "Does not tie", "Stale value"}
                         and i["cell"] in formulas}

    def tot(role, line):
        return sum(val(roles[role], idx[(b, line)]) or 0 for b in bus if (b, line) in idx)

    rev, gm, oi = tot("outlook", "Revenue"), tot("outlook", "Gross Margin"), tot("outlook", "Operating Income")
    rev_y = tot("yoy_base", "Revenue")
    shown_rev = val(roles["outlook"], idx[("Total", "Revenue")]) if ("Total", "Revenue") in idx else None

    by_cat = Counter(c for i in issue_list for c in i["categories"])
    by_sev = Counter(i["severity"] for i in issue_list)

    return {
        "file": filename,
        "period": {k: labels.get(k) for k in VALUE_ROLES},
        "outlook_quarter": outlook_q,
        "basis": basis,
        "principles_fy": fy,
        "threshold": threshold,
        "summary": {
            "line_items": len(rows),
            "formula_cells": formula_cells,
            "formula_accuracy": round(1 - len(bad_formula_cells) / formula_cells, 3) if formula_cells else 1.0,
            "issues": len(issue_list),
            "by_severity": {k: by_sev.get(k, 0) for k in ("high", "medium", "low")},
            "by_category": dict(by_cat.most_common()),
            "revenue": rev, "revenue_yoy": rev / rev_y - 1 if rev_y else None,
            "gm_pct": gm / rev if rev else None, "operating_income": oi,
            "shown_total_revenue": shown_rev,
        },
        "issues": issue_list,
        "movers": movers,
        "commentary": comm,
        "anomalies": anomalies,
        "history_note": history_note,
    }


Z_HIGH = 5.0


def _cell_sort(cell):
    m = re.match(r"([A-Z]+)(\d+)", cell)
    return (int(m.group(2)), m.group(1)) if m else (0, cell)
