"""把处理结果导出成一个自包含的静态页面（dist/index.html）。

为什么要静态化：
    FastAPI 那套需要一台一直开着的机器；而这个 demo 的数据是批处理产物，
    跑完管线就不再变化。把它压成一个数据内联的 HTML 之后：
      - 可以直接双击打开（file:// 也能用，不依赖服务器）
      - 可以丢到任何静态托管（GitHub Pages / Cloudflare Pages / OSS / Nginx）
      - 可以当附件发给人，对方打开就能看到完整交互
    这才是"随时在线查看"最省事的形态。

用法:
    python -m src.export_static
    python -m src.export_static --out dist --balance-days 180
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT

TEMPLATE = PROJECT_ROOT / "app" / "static" / "static_template.html"
PLACEHOLDER = "/*__AGRI_DATA__*/ null"

VENDOR_DIR = PROJECT_ROOT / "app" / "static" / "vendor"

# 占位符 -> (本地依赖文件名, 找不到本地依赖时的 CDN 兜底)
VENDOR_SLOTS = {
    "/*__LEAFLET_CSS__*/": (
        "leaflet.css",
        '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />',
    ),
    "/*__LEAFLET_JS__*/": (
        "leaflet.js",
        '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>',
    ),
    "/*__CHART_JS__*/": (
        "chart.umd.js",
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>',
    ),
}


def log(msg: str) -> None:
    print("[export] " + str(msg), flush=True)


def _read_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def _records(df: pd.DataFrame | None, cols: list[str] | None = None,
             ndigits: int | None = None) -> list[dict]:
    if df is None or df.empty:
        return []
    out = df.copy()
    if cols:
        out = out[[c for c in cols if c in out.columns]]
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    recs = json.loads(out.to_json(orient="records", force_ascii=False, date_format="iso"))
    if ndigits is not None:
        recs = [{k: (round(v, ndigits) if isinstance(v, float) else v) for k, v in r.items()}
                for r in recs]
    return recs


def _group_by_plot(recs: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in recs:
        out.setdefault(str(r.get("plot_id")), []).append(r)
    return out


def build_payload(balance_days: int = 180) -> dict:
    processed = PROJECT_ROOT / "data" / "processed"
    raw = PROJECT_ROOT / "data" / "raw"

    status = _read_csv(processed / "plot_status.csv")
    pheno = _read_csv(processed / "phenology.csv")
    plan = _read_csv(processed / "irrigation_plan.csv")
    balance = _read_csv(processed / "daily_balance.csv")
    importance = _read_csv(processed / "feature_importance.csv")
    ts = _read_csv(raw / "plot_timeseries.csv")

    metrics: dict = {"available": False}
    mp = processed / "model_metrics.json"
    if mp.exists():
        with open(mp, "r", encoding="utf-8") as fh:
            metrics = json.load(fh)
        metrics["available"] = True

    geojson: dict = {"type": "FeatureCollection", "features": []}
    gp = processed / "plots.geojson"
    if gp.exists():
        with open(gp, "r", encoding="utf-8") as fh:
            geojson = json.load(fh)

    plots = _records(status)

    # 时序：只保留画曲线需要的列，避免把整个 CSV 塞进 HTML
    ts_cols = [c for c in ["plot_id", "date", "NDVI", "NDRE", "EVI", "valid_frac"] if c is not None]
    ts_recs = _records(ts, cols=ts_cols, ndigits=4) if ts is not None else []

    # 水分平衡只保留最近 N 天，否则 12 地块 x 400 天会把页面撑到几 MB
    bal_recs: list[dict] = []
    if balance is not None and not balance.empty:
        balance = balance.copy()
        if pd.api.types.is_datetime64_any_dtype(balance["date"]):
            balance["date"] = balance["date"].dt.strftime("%Y-%m-%d")
        keep = [c for c in ["plot_id", "date", "dr_mm", "raw_mm", "taw_mm",
                            "etc", "rain"] if c in balance.columns]
        for pid, grp in balance.groupby("plot_id"):
            bal_recs.extend(_records(grp[keep].tail(balance_days), ndigits=3))

    plan_recs = _records(plan, ndigits=2)

    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "n_plots": len(plots),
            "source": "Sentinel-2 时序 + 实测数据，离线管线导出",
        },
        "geojson": geojson,
        "plots": plots,
        "timeseries": _group_by_plot(ts_recs),
        "balance": _group_by_plot(bal_recs),
        "plan": _group_by_plot(plan_recs),
        "phenology": _group_by_plot(_records(pheno, ndigits=2)),
        "metrics": metrics,
        "importance": _records(importance, ndigits=4),
    }


def inline_vendor(html: str) -> tuple[str, list[str]]:
    """把 Leaflet / Chart.js 内联进页面。

    内联之后页面就只剩地图瓦片一个外部依赖——瓦片挂了，地块着色、
    曲线、灌溉处方照样能用。对"发给别人打开就能看"这个场景很关键。
    """
    used: list[str] = []
    for slot, (fname, cdn) in VENDOR_SLOTS.items():
        if slot not in html:
            continue
        path = VENDOR_DIR / fname
        if path.exists() and path.stat().st_size > 0:
            content = path.read_text(encoding="utf-8", errors="replace")
            if "</script" in content and fname.endswith(".js"):
                # 理论上不会发生，真发生了就退回 CDN，别把页面写坏
                log("警告: " + fname + " 含 </script，改用 CDN 引用")
                html = html.replace(slot, cdn)
                continue
            html = html.replace(slot, content)
            used.append(fname)
        else:
            html = html.replace(slot, cdn)
    return html, used


def render(payload: dict, out_dir: Path) -> Path:
    if not TEMPLATE.exists():
        raise FileNotFoundError("找不到页面模板: " + str(TEMPLATE))
    html = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise ValueError("模板里找不到数据占位符: " + PLACEHOLDER)

    html, vendored = inline_vendor(html)
    if vendored:
        log("已内联依赖: " + ", ".join(vendored))
    else:
        log("未找到本地依赖，页面将走 CDN（国内可能加载不出来，建议先跑 tools/fetch_vendor.py）")

    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里出现 </script> 提前闭合脚本块
    data = data.replace("</", "<\\/")

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "index.html"
    out.write_text(html.replace(PLACEHOLDER, data), encoding="utf-8")

    # 顺带把原始 GeoJSON 也放一份，方便别的工具复用
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    with open(out_dir / "data" / "plots.geojson", "w", encoding="utf-8") as fh:
        json.dump(payload["geojson"], fh, ensure_ascii=False)
    with open(out_dir / "data" / "summary.json", "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in payload.items() if k != "geojson"},
                  fh, ensure_ascii=False, separators=(",", ":"))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出自包含静态演示页")
    parser.add_argument("--out", default="dist", help="输出目录，默认 dist")
    parser.add_argument("--balance-days", type=int, default=180,
                        help="水分平衡曲线保留最近多少天")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir

    payload = build_payload(balance_days=args.balance_days)
    if not payload["plots"]:
        log("data/processed 里没有结果，请先运行: python -m src.pipeline")
        return 2

    out = render(payload, out_dir)
    size_kb = out.stat().st_size / 1024.0
    log("地块 " + str(len(payload["plots"])) + " 个, 时序 "
        + str(len(payload["timeseries"])) + " 条曲线, 灌溉计划 "
        + str(len(payload["plan"])) + " 个地块有记录")
    log("已生成 " + str(out) + "  (" + format(size_kb, ".0f") + " KB)")
    log("可以直接双击打开，或把 " + str(out_dir) + " 整个目录丢到任何静态托管")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
