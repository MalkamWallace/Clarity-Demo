"""Clarity Demo API and static frontend."""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

from . import checker, forecaster
from . import data_store as ds

app = FastAPI(title="Clarity Demo", version="1.0")
STATIC = Path(__file__).resolve().parents[1] / "static"
MAX_BYTES = 5 * 1024 * 1024


async def _book_bytes(file: UploadFile | None, sample: str | None) -> tuple[bytes, str]:
    if file is not None and file.filename:
        if not file.filename.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(400, "Upload an .xlsx control book.")
        data = await file.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(400, "File is larger than 5 MB.")
        return data, file.filename
    key = sample or "with_issues"
    if key not in ds.SAMPLE_FILES:
        raise HTTPException(400, "Unknown sample.")
    name = ds.SAMPLE_FILES[key][0]
    return (ds.SAMPLES / name).read_bytes(), name


@app.get("/api/catalogue")
def catalogue():
    return ds.catalogue()


@app.get("/api/samples/{key}")
def sample_file(key: str):
    if key not in ds.SAMPLE_FILES:
        raise HTTPException(404)
    name = ds.SAMPLE_FILES[key][0]
    return FileResponse(ds.SAMPLES / name, filename=name)


@app.get("/api/history.csv")
def history_csv():
    return FileResponse(ds.DATA / "history.csv", filename="meridian_history_FY22_FY26.csv")


@app.post("/api/check")
async def check(file: UploadFile | None = File(None), sample: str | None = Form(None),
                basis: str = Form("yoy"), principles_fy: str | None = Form(None)):
    if basis not in ("yoy", "qoq", "plan"):
        raise HTTPException(400, "basis must be yoy, qoq or plan")
    data, name = await _book_bytes(file, sample)
    try:
        return checker.analyze(data, basis=basis, principles_fy=principles_fy or None, filename=name)
    except checker.ControlBookError as e:
        raise HTTPException(422, str(e))


SEV_FILL = {"high": "F8C9C4", "medium": "FCE7B2", "low": "DDEBF7"}


@app.post("/api/check/export")
async def check_export(file: UploadFile | None = File(None), sample: str | None = Form(None),
                       basis: str = Form("yoy")):
    """Return the reviewed workbook: flagged cells highlighted with notes, plus a review sheet."""
    data, name = await _book_bytes(file, sample)
    try:
        res = checker.analyze(data, basis=basis, filename=name)
    except checker.ControlBookError as e:
        raise HTTPException(422, str(e))
    wb = load_workbook(io.BytesIO(data))
    ws = wb.worksheets[0]
    for i in res["issues"]:
        c = ws[i["cell"]]
        c.fill = PatternFill("solid", fgColor=SEV_FILL[i["severity"]])
        c.comment = Comment("Clarity: " + " | ".join(i["details"]), "Clarity Demo", width=320, height=140)

    rv = wb.create_sheet("Clarity Review", 0)
    blue = PatternFill("solid", fgColor="0672CB")
    rv.append(["Clarity Demo review", name])
    rv["A1"].font = Font(bold=True, size=14, color="0B2D5B")
    s = res["summary"]
    rv.append(["Formula accuracy", f"{s['formula_accuracy']:.1%}"])
    rv.append(["Issues (high / medium)", f"{s['by_severity']['high']} / {s['by_severity']['medium']}"])
    rv.append([])
    rv.append(["Checklist", "", "", "", "", "Reviewed?"])
    rv.append(["Severity", "Cell", "Business unit", "Line item", "Issue", "", "Detail"])
    for c in rv[rv.max_row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = blue
    for i in res["issues"]:
        rv.append([i["severity"].upper(), i["cell"], i["bu"], i["line"], ", ".join(i["categories"]), "☐",
                   " | ".join(i["details"])])
    rv.append([])
    rv.append([f"Key movers and commentary ({basis.upper()})"])
    rv[rv.max_row][0].font = Font(bold=True, color="0B2D5B")
    for b in res["commentary"]["bullets"]:
        rv.append(["•", b])
    for col, w in zip("ABCDEFG", [10, 8, 20, 18, 34, 10, 110]):
        rv.column_dimensions[col].width = w

    out = io.BytesIO()
    wb.save(out)
    fname = name.rsplit(".", 1)[0] + "_clarity_review.xlsx"
    return Response(out.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/forecast")
async def forecast(bu: str = Form("Total"), horizon: int = Form(4), principles_fy: str = Form("FY27"),
                   file: UploadFile | None = File(None)):
    uploaded = None
    if file is not None and file.filename:
        try:
            uploaded = pd.read_csv(io.BytesIO(await file.read()))
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Could not read the CSV.")
        need = {"quarter", "bu", "line", "value"}
        if not need <= set(uploaded.columns):
            raise HTTPException(422, "CSV needs columns: quarter, bu, line, value (same as the history download).")
        uploaded["qkey"] = uploaded["quarter"].map(ds.quarter_key)
    try:
        return forecaster.run(bu=bu, horizon=horizon, principles_fy=principles_fy, uploaded=uploaded)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/healthz")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
