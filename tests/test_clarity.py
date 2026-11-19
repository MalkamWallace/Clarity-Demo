"""Run with:  python -m pytest -q"""
from pathlib import Path

from fastapi.testclient import TestClient

from app import checker, forecaster
from app.formula_eval import Evaluator, relative_signature, references
from app.main import app

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
client = TestClient(app)


def _issues(name):
    res = checker.analyze((SAMPLES / name).read_bytes())
    return res, {(i["cell"], c) for i in res["issues"] for c in i["categories"]}


def test_formula_evaluator():
    vals = {"A1": 10, "A2": 4, "A3": 0}
    ev = Evaluator(vals.get)
    assert ev.eval_formula("=A1-A2") == 6
    assert ev.eval_formula("=SUM(A1:A3)") == 14
    assert ev.eval_formula("=IFERROR(A1/A3,0)") == 0
    assert references("=SUM(F10,F18,F10)") == ["F10", "F18", "F10"]
    assert relative_signature("=G7/D7", "H7") == relative_signature("=G9/D9", "H9")


def test_clean_book_has_no_issues():
    res, _ = _issues("control_book_FY27Q1_clean.xlsx")
    assert res["summary"]["issues"] == 0
    assert res["summary"]["formula_accuracy"] == 1.0
    assert len(res["movers"]) == 10


def test_planted_issues_are_all_caught():
    _, found = _issues("control_book_FY27Q1_with_issues.xlsx")
    expected = {
        ("F17", "Hardcoded value"),          # typed-over Gross Margin
        ("F17", "Does not tie"),
        ("H10", "Inconsistent formula"),     # QoQ % pointing at the wrong row
        ("F31", "Inconsistent formula"),     # Total Revenue missing a BU
        ("F31", "Does not tie"),
        ("F34", "Repeated calculation"),     # Total Opex double counts Devices
        ("L20", "Broken formula"),           # #REF!
        ("F24", "Anomaly vs history"),       # S&S COGS spike
        ("F20", "Anomaly vs history"),       # Infra units drop
        ("F10", "Budget variance"),          # Devices Opex over budget
    }
    missing = expected - found
    assert not missing, f"missed: {missing}"


def test_top_mover_is_the_cogs_spike():
    res, _ = _issues("control_book_FY27Q1_with_issues.xlsx")
    top = res["movers"][0]
    assert (top["bu"], top["line"]) == ("Software & Services", "COGS")
    assert top["anomaly"]


def test_forecast_ties_and_respects_horizon():
    res = forecaster.run("Total", 8, "FY27")
    assert len(res["table"]) == 8
    for row in res["table"]:
        assert abs(row["Gross Margin"] - (row["Revenue"] - row["COGS"])) < 0.2
        assert abs(row["Operating Income"] - (row["Gross Margin"] - row["Opex"])) < 0.2
    assert {c["metric"] for c in res["principle_checks"]} == {"Revenue growth", "Gross margin %", "Opex % of revenue"}
    assert res["backtest_accuracy"] > 0.9


def test_api_endpoints():
    assert client.get("/api/catalogue").status_code == 200
    r = client.post("/api/check", data={"sample": "with_issues", "basis": "qoq"})
    assert r.status_code == 200 and r.json()["basis"] == "qoq"
    r = client.post("/api/check/export", data={"sample": "with_issues"})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.post("/api/forecast", data={"bu": "Devices", "horizon": 3, "principles_fy": "FY27"})
    assert r.status_code == 200 and len(r.json()["table"]) == 3
    r = client.post("/api/check", files={"file": ("x.xlsx", b"not a workbook")})
    assert r.status_code == 422
