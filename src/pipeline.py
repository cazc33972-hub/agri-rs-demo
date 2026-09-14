"""一键跑通整条链路：时序重建 -> 物候 -> 建模 -> 灌溉处方。

用法:
    python -m src.pipeline --config config/config.yaml
    python -m src.pipeline --skip-irrigation          # 没网 / 不想拉气象数据时
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

from .config import PROJECT_ROOT, load_config
from .et0 import fetch_nasa_power, load_weather_csv
from .features import build_feature_table
from .io_utils import (
    ensure_dirs,
    filter_by_quality,
    load_ground_truth,
    load_timeseries,
    save_table,
)
from .irrigation import (
    build_daily_driver,
    build_plan,
    forecast_prescription,
    simulate_water_balance,
)
from .model import train_and_evaluate

# 质量过滤阈值：有效像元占比低于这个值的观测直接丢掉
MIN_VALID_FRAC = 0.3
# 水分平衡只回溯这么久，保证处方针对"当前"而不是三年前
LOOKBACK_DAYS = 400


def log(msg: str) -> None:
    print("[pipeline] " + str(msg), flush=True)


def resolve_output_dirs() -> dict:
    dirs = {
        "raw": PROJECT_ROOT / "data" / "raw",
        "interim": PROJECT_ROOT / "data" / "interim",
        "processed": PROJECT_ROOT / "data" / "processed",
        "figures": PROJECT_ROOT / "reports" / "figures",
    }
    ensure_dirs(*dirs.values())
    return dirs


# ----------------------------------------------------------------------
# ②③ 时序重建 + 物候提取 -> 特征表
# ----------------------------------------------------------------------
def step_features(cfg, ts: pd.DataFrame, dirs: dict) -> pd.DataFrame:
    log("开始提取物候与特征 ...")
    features, phenology = build_feature_table(ts, cfg, index_col="NDVI")

    if features.empty:
        raise RuntimeError(
            "特征表为空。最常见的原因：时序数据太少，或 min_obs_per_year 门槛过高。"
        )

    features = df_to_json_safe(features)
    phenology = df_to_json_safe(phenology)
    save_table(features, dirs["processed"] / "features.csv")
    save_table(phenology, dirs["processed"] / "phenology.csv")

    n_crop = int(features["PHENO_is_crop"].sum()) if "PHENO_is_crop" in features else 0
    log("特征表: " + str(features.shape[0]) + " 个 地块-年, "
        + str(features.shape[1]) + " 列; 判定有作物的 " + str(n_crop) + " 个")
    return features


# ----------------------------------------------------------------------
# ④ 建模
# ----------------------------------------------------------------------
def step_model(cfg, features: pd.DataFrame, dirs: dict) -> dict:
    gt_path = dirs["raw"] / "ground_truth.csv"
    if not gt_path.exists():
        log("没有找到 " + str(gt_path) + "，跳过建模。")
        log("提示: 实测表需要 plot_id, year, 以及在 config 里 model.target 指定的列。")
        return {"skipped": "缺少实测数据文件"}

    gt = load_ground_truth(gt_path)
    log("实测数据: " + str(len(gt)) + " 条")
    result = train_and_evaluate(features, gt, cfg)

    if result.get("skipped"):
        log("建模跳过: " + str(result["skipped"]))
        return result

    preds = df_to_json_safe(result["predictions"])
    save_table(preds, dirs["processed"] / "predictions.csv")
    if result.get("importance") is not None:
        save_table(result["importance"], dirs["processed"] / "feature_importance.csv")

    metrics = {
        "task": result.get("task"),
        "target": result.get("target"),
        "n_samples": result.get("n_samples"),
        "n_features": result.get("n_features"),
        "group_col": result.get("group_col"),
        "n_groups": result.get("n_groups"),
        "cv": result.get("metrics_cv", {}),
        "holdout": result.get("metrics_holdout", {}),
        "holdout_years": result.get("holdout_years"),
        "warnings": {k: v for k, v in result.items() if k.endswith("_warning")},
    }
    with open(dirs["processed"] / "model_metrics.json", "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, ensure_ascii=False, indent=2)

    log("交叉验证精度: " + json.dumps(metrics["cv"], ensure_ascii=False))
    if metrics["holdout"]:
        log("留出年精度: " + json.dumps(metrics["holdout"], ensure_ascii=False))
    if result.get("importance") is not None:
        top = result["importance"].head(5)["feature"].tolist()
        log("Top5 重要特征: " + ", ".join(top))
    return result


# ----------------------------------------------------------------------
# ⑤ 灌溉处方
# ----------------------------------------------------------------------
def step_irrigation(cfg, ts: pd.DataFrame, dirs: dict,
                    features: pd.DataFrame | None = None) -> list[dict]:
    lat = cfg.get("study_area.lat")
    lon = cfg.get("study_area.lon")
    if lat is None or lon is None:
        log("config 里缺少 study_area.lat / lon，跳过灌溉决策。")
        return []

    cfg_start = str(cfg.get("time.start", "2022-01-01"))
    cfg_end = str(cfg.get("time.end", "2024-12-31"))
    elev = float(cfg.get("study_area.elev_m", 0.0) or 0.0)
    local_csv = cfg.get("irrigation.et0_csv", None)

    if local_csv:
        # 用本地气象站 / 离线测试数据，不联网
        path = Path(str(local_csv))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        log("读取本地气象数据: " + str(path))
        weather = load_weather_csv(path, float(lat), elev)
    else:
        cache = dirs["raw"] / "weather_nasa_power.csv"
        log("拉取 NASA POWER 日气象数据 ...")
        try:
            weather = fetch_nasa_power(lat, lon, cfg_start, cfg_end, cache_path=cache)
        except Exception as exc:
            log("NASA POWER 拉取失败（" + repr(exc) + "），跳过灌溉决策。")
            return []

    if weather.empty:
        log("气象数据为空，跳过灌溉决策。")
        return []

    # 只保留最近的窗口，让处方针对当前状态
    cutoff = weather["date"].max() - pd.Timedelta(LOOKBACK_DAYS, unit="D")
    weather = weather[weather["date"] >= cutoff].reset_index(drop=True)
    log("气象窗口: " + str(weather["date"].min().date()) + " ~ "
        + str(weather["date"].max().date()) + "，共 " + str(len(weather)) + " 天")

    plans, statuses, balances = [], [], []
    for plot_id in sorted(ts["plot_id"].unique()):
        try:
            driver = build_daily_driver(ts, weather, cfg, plot_id=plot_id)
            if driver.empty:
                continue
            balance = simulate_water_balance(driver, cfg)
            plan = build_plan(balance)
            if not plan.empty:
                plan.insert(0, "plot_id", plot_id)
                plans.append(plan)

            # 状态日期取"最近一次有 NDVI 观测的那天"，而不是气象窗口的最后一天。
            # 否则越冬期/休耕期会得到一屏灰地块，地图完全没有信息量。
            # 真实场景里最新一景 Sentinel-2 也就是几天前，本来就该以它为准。
            obs = balance[balance["ndvi"].notna()]
            last = obs.iloc[-1] if not obs.empty else balance.iloc[-1]

            # 处方也只看到观测日为止，保证"状态"和"结论"是同一个时点
            upto = balance[balance["date"] <= last["date"]]
            fc = forecast_prescription(upto, cfg)
            balances.append(balance.assign(plot_id=plot_id)[
                ["plot_id", "date", "ndvi", "kc", "et0", "etc", "rain",
                 "dr_mm", "taw_mm", "raw_mm", "irrigation_gross_mm"]
            ])
            statuses.append({
                "plot_id": plot_id,
                "date": str(pd.Timestamp(last["date"]).date()),
                "ndvi": None if pd.isna(last["ndvi"]) else round(float(last["ndvi"]), 4),
                "kc": round(float(last["kc"]), 3),
                "etc_mm_per_day": round(float(last["etc"]), 2),
                "dr_mm": round(float(last["dr_mm"]), 1),
                "taw_mm": round(float(last["taw_mm"]), 1),
                "raw_mm": round(float(last["raw_mm"]), 1),
                "dr_ratio": round(float(last["dr_ratio"]), 3),
                "stressed": bool(last["stressed"]),
                "needs_irrigation": bool(fc.get("needs_irrigation")),
                "days_until_irrigation": fc.get("days_until"),
                "advice": fc.get("reason"),
                "irrigation_gross_mm": fc.get("gross_irrigation_mm", 0.0),
            })
        except Exception as exc:  # 单块地出问题不该拖垮整条管线
            log("地块 " + str(plot_id) + " 水分平衡失败: " + repr(exc))

    if plans:
        all_plans = pd.concat(plans, ignore_index=True)
        save_table(all_plans, dirs["processed"] / "irrigation_plan.csv")
        log("灌溉处方: " + str(len(all_plans)) + " 条灌溉事件")
    else:
        log("没有生成任何灌溉事件（可能是降水充足或 Kc 一直偏低）。")

    if balances:
        save_table(pd.concat(balances, ignore_index=True),
                   dirs["processed"] / "daily_balance.csv")

    if statuses:
        attach_season_stats(statuses, features)
        status = pd.DataFrame(statuses)
        save_table(status, dirs["processed"] / "plot_status.csv")
        n_need = int(status["needs_irrigation"].sum())
        log("地块状态: 共 " + str(len(status)) + " 块, 其中 " + str(n_need) + " 块需要灌溉")
        write_geojson(cfg, statuses, dirs)
    return statuses


# 物候/长势特征列 -> 状态表列名，以及各自的小数位
SEASON_STAT_COLUMNS = [
    ("NDVI_max", "ndvi_peak", 4),                  # 生长季峰值，最直观的长势指标
    ("PHENO_NDVI_integral", "ndvi_integral", 1),   # 生长季累积（NDVI·天）
    ("PHENO_amplitude", "ndvi_amplitude", 4),
    ("PHENO_SOS_doy", "sos_doy", 0),
    ("PHENO_EOS_doy", "eos_doy", 0),
    ("PHENO_LOS", "los_days", 1),
    ("PHENO_is_crop", "is_crop", None),
]


def attach_season_stats(statuses: list[dict], features: pd.DataFrame | None) -> None:
    """把最近一个生长季的长势特征并进地块状态。

    为什么需要：地图如果只按"最新一景 NDVI"着色，成熟后期各块地都是 0.1 上下，
    整张图一片红，看不出任何差异。而长势分级真正该用的是生长季峰值或累积量——
    这也是农学上的常规做法（用物候期的极值/积分而不是单时相）。
    两个指标都放进去，前端可以切换。
    """
    if features is None or features.empty or "plot_id" not in features.columns:
        return
    if "year" not in features.columns:
        return

    latest = features.sort_values("year").groupby("plot_id").tail(1).set_index("plot_id")

    for s in statuses:
        pid = str(s["plot_id"])
        if pid not in latest.index:
            continue
        row = latest.loc[pid]
        for src, dst, nd in SEASON_STAT_COLUMNS:
            if src not in row.index:
                continue
            val = row[src]
            if pd.isna(val):
                s[dst] = None
            elif nd is None:
                s[dst] = bool(val)
            else:
                s[dst] = round(float(val), nd)
        s["season_year"] = int(row["year"]) if "year" in row.index else None


# ----------------------------------------------------------------------
# 给 Web demo 用的 GeoJSON
# ----------------------------------------------------------------------
def write_geojson(cfg, statuses: list[dict], dirs: dict) -> None:
    """把地块矢量与状态表合起来，输出前端直接吃的 GeoJSON。

    注意传进来的是纯 Python dict 列表，不要传 DataFrame ——
    iterrows() 会给出 numpy 标量，json.dump 序列化 np.bool_ / np.int64 会直接抛错。
    """
    src = cfg.resolve("study_area.geojson", "config/study_area.geojson")
    if not Path(src).exists():
        log("找不到地块矢量 " + str(src) + "，跳过 GeoJSON 输出。")
        return
    with open(src, "r", encoding="utf-8") as fh:
        gj = json.load(fh)

    lookup = {str(s["plot_id"]): s for s in statuses}
    kept = []
    for feat in gj.get("features", []):
        pid = str(feat.get("properties", {}).get("plot_id", ""))
        if pid in lookup:
            feat.setdefault("properties", {}).update(lookup[pid])
        kept.append(feat)
    gj["features"] = kept

    out = dirs["processed"] / "plots.geojson"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(gj, fh, ensure_ascii=False)
    log("Web 用 GeoJSON 已写出: " + str(out))


def df_to_json_safe(df: pd.DataFrame) -> pd.DataFrame:
    """把 Timestamp / NA 转成可安全写 CSV/JSON 的形式。"""
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    return out


# ----------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="农田长势遥感与灌溉决策管线")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--timeseries", default=None,
                        help="覆盖默认的 data/raw/plot_timeseries.csv")
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-irrigation", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    dirs = resolve_output_dirs()

    ts_path = Path(args.timeseries) if args.timeseries else dirs["raw"] / "plot_timeseries.csv"
    if not ts_path.exists():
        log("找不到时序文件: " + str(ts_path))
        log("请先在 GEE Code Editor 里跑 gee/01_export_timeseries.js，")
        log("把导出的 CSV 下载到 data/raw/plot_timeseries.csv")
        return 2

    log("读取时序: " + str(ts_path))
    ts = load_timeseries(ts_path)
    log("原始观测 " + str(len(ts)) + " 条, 地块 " + str(ts["plot_id"].nunique())
        + " 个, 时间 " + str(ts["date"].min().date()) + " ~ " + str(ts["date"].max().date()))

    ts = filter_by_quality(ts, MIN_VALID_FRAC)
    log("质量过滤后 " + str(len(ts)) + " 条")

    features = step_features(cfg, ts, dirs)

    if not args.skip_model:
        step_model(cfg, features, dirs)

    if not args.skip_irrigation:
        step_irrigation(cfg, ts, dirs, features)

    log("完成。输出目录: " + str(dirs["processed"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
