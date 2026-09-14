"""FastAPI 后端：把管线的产物暴露成 demo 用的接口。

启动:
    uvicorn app.main:app --reload --port 8000
然后打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from src.config import PROJECT_ROOT

PROCESSED = PROJECT_ROOT / "data" / "processed"
RAW = PROJECT_ROOT / "data" / "raw"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="农田长势遥感与灌溉决策 Demo", version="0.1.0")

_cache: dict[str, Any] = {}


def read_csv(name: str, folder: Path = PROCESSED) -> pd.DataFrame | None:
    p = folder / name
    if not p.exists():
        return None
    key = str(p)
    if key not in _cache:
        _cache[key] = pd.read_csv(p, encoding="utf-8-sig")
    return _cache[key]


def records(df: pd.DataFrame | None) -> list[dict]:
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def missing(name: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail="缺少 " + name + "，请先运行: python -m src.pipeline",
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    """一眼看出哪些产物已经生成，方便排查。"""
    expected = [
        "plot_status.csv", "phenology.csv", "features.csv",
        "irrigation_plan.csv", "daily_balance.csv",
        "predictions.csv", "model_metrics.json", "plots.geojson",
    ]
    return {
        "ok": True,
        "processed_dir": str(PROCESSED),
        "artifacts": {n: (PROCESSED / n).exists() for n in expected},
    }


@app.get("/api/plots")
def plots() -> dict:
    p = PROCESSED / "plots.geojson"
    if not p.exists():
        raise missing("plots.geojson")
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/api/status")
def status() -> list[dict]:
    return records(read_csv("plot_status.csv"))


@app.get("/api/timeseries/{plot_id}")
def timeseries(plot_id: str) -> list[dict]:
    """给前端画曲线用。直接读原始时序表（demo 规模完全扛得住）。"""
    df = read_csv("plot_timeseries.csv", folder=RAW)
    if df is None:
        raise missing("data/raw/plot_timeseries.csv")
    sub = df[df["plot_id"].astype(str) == str(plot_id)].copy()
    if sub.empty:
        return []
    keep = [c for c in ["date", "NDVI", "EVI", "SAVI", "NDRE", "NDWI",
                        "NDVI_smooth", "valid_frac"] if c in sub.columns]
    sub = sub[keep].sort_values("date")
    return records(sub)


@app.get("/api/phenology/{plot_id}")
def phenology(plot_id: str) -> list[dict]:
    df = read_csv("phenology.csv")
    if df is None:
        raise missing("data/processed/phenology.csv")
    return records(df[df["plot_id"].astype(str) == str(plot_id)])


@app.get("/api/plan/{plot_id}")
def plan(plot_id: str) -> list[dict]:
    df = read_csv("irrigation_plan.csv")
    if df is None:
        return []
    return records(df[df["plot_id"].astype(str) == str(plot_id)])


@app.get("/api/balance/{plot_id}")
def balance(plot_id: str) -> list[dict]:
    df = read_csv("daily_balance.csv")
    if df is None:
        return []
    sub = df[df["plot_id"].astype(str) == str(plot_id)].tail(180)
    return records(sub)


@app.get("/api/metrics")
def metrics() -> dict:
    p = PROCESSED / "model_metrics.json"
    if not p.exists():
        return {"available": False}
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["available"] = True
    return data


@app.get("/api/importance")
def importance() -> list[dict]:
    return records(read_csv("feature_importance.csv"))
