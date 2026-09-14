"""生成合成数据，用于在没有真实数据时跑通整条管线（也是本项目的自测数据）。

产出:
    config/study_area.geojson      —— 12 个虚构地块（仅当文件不存在时创建）
    data/raw/plot_timeseries.csv   —— 模拟 GEE 导出的地块指数时序
    data/raw/ground_truth.csv      —— 模拟实测产量
    data/raw/weather_synthetic.csv —— 模拟 NASA POWER 日气象（离线测试灌溉模块）

合成逻辑：每个「地块-年」一条双逻辑斯蒂生长曲线 + 噪声 + 随机缺测；
产量由生长季积分与峰值共同决定，所以模型应该能学到真实关系，
用来验证管线是否正常，而不是用来吹精度。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 允许 `python tools/make_synthetic_data.py` 这种直接调用方式
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import PROJECT_ROOT

PLOTS = ["P%03d" % i for i in range(1, 13)]
YEARS = [2022, 2023, 2024]
CENTER_LAT, CENTER_LON = 36.3015, 116.1045
GRID = (4, 3)          # 4 行 x 3 列 = 12 块


def logistic(t, mn, mx, m_s, s, m_a, a):
    return mn + (mx - mn) * (
        1.0 / (1.0 + np.exp(-m_s * (t - s))) - 1.0 / (1.0 + np.exp(-m_a * (t - a)))
    )


def build_geometry() -> dict:
    """排成 4x3 网格的 12 个 200m x 200m 地块，够 demo 用了。"""
    rows, cols = GRID
    d = 0.002          # 约 200 m
    gap = 0.0006
    lat0 = CENTER_LAT - rows * (d + gap) / 2.0
    lon0 = CENTER_LON - cols * (d + gap) / 2.0

    features = []
    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            pid = PLOTS[idx]
            y0 = lat0 + r * (d + gap)
            x0 = lon0 + c * (d + gap)
            ring = [[x0, y0], [x0 + d, y0], [x0 + d, y0 + d], [x0, y0 + d], [x0, y0]]
            features.append({
                "type": "Feature",
                "properties": {
                    "plot_id": pid,
                    "crop": "winter_wheat",
                    "area_ha": round(d * 111000 * d * 111000 * 0.7 / 10000, 2),
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            })
    return {
        "type": "FeatureCollection",
        "name": "study_area_synthetic",
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def build_timeseries(rng) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, truth = [], []

    for plot in PLOTS:
        plot_quality = rng.uniform(0.85, 1.10)      # 地块本身的肥力差异

        for year in YEARS:
            t0 = pd.Timestamp(year=year, month=2, day=1)
            t1 = pd.Timestamp(year=year, month=7, day=15)
            dates = pd.date_range(t0, t1, freq="5D")
            t = (dates - t0).days.to_numpy(dtype=float)

            # 返青期、成熟期逐年浮动，模拟播期与管理差异
            sos = rng.uniform(35, 50)
            eos = rng.uniform(118, 142)
            peak = plot_quality * rng.uniform(0.80, 0.90)

            base = logistic(t, 0.12, peak, 0.12, sos, 0.10, eos)
            base = base + rng.normal(0, 0.012, size=len(t))

            for i, d in enumerate(dates):
                if rng.random() < 0.30:             # 30% 被云污染，模拟真实缺测
                    continue
                v = float(np.clip(base[i], -0.1, 1.0))
                rows.append({
                    "plot_id": plot,
                    "date": d.strftime("%Y-%m-%d"),
                    "n_pixels": int(rng.integers(60, 120)),
                    "valid_frac": float(np.clip(rng.normal(0.75, 0.15), 0.1, 1.0)),
                    "NDVI": round(v, 4),
                    "EVI": round(v * 0.85 + rng.normal(0, 0.010), 4),
                    "SAVI": round(v * 1.15 + rng.normal(0, 0.010), 4),
                    "NDRE": round(v * 0.55 + rng.normal(0, 0.012), 4),
                    "NDWI": round(-v * 0.25 + rng.normal(0, 0.010), 4),
                })

            # 产量由生长季累积（积分）与峰值共同决定，噪声刻意压小，
            # 这样模型应该能学出来——用来验证管线是否跑通，不是真实精度声明。
            integral = float(np.trapezoid(base - 0.12, dx=5.0))
            yield_ = 3.0 + 0.0045 * integral + 1.5 * (peak - 0.75) \
                + rng.normal(0, 0.08)
            truth.append({"plot_id": plot, "year": year,
                          "yield": round(float(yield_), 3)})

    return pd.DataFrame(rows), pd.DataFrame(truth)


def build_weather(start="2022-01-01", end="2024-12-31", seed=7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, end, freq="D")
    doy = dates.dayofyear.to_numpy()

    tmax = 14 + 14 * np.sin(2 * np.pi * (doy - 105) / 365) + rng.normal(0, 2.5, len(dates))
    tmin = tmax - rng.uniform(7, 12, len(dates))
    rh = np.clip(62 + rng.normal(0, 10, len(dates)), 20, 98)
    u2 = np.clip(rng.normal(2.2, 0.8, len(dates)), 0.4, 7.0)
    rs = np.clip(16 + 7 * np.sin(2 * np.pi * (doy - 100) / 365)
                 + rng.normal(0, 3.0, len(dates)), 2, 30)
    rain = np.where(rng.random(len(dates)) < 0.20, rng.gamma(1.4, 5.0, len(dates)), 0.0)

    return pd.DataFrame({
        "date": dates, "tmax": np.round(tmax, 2), "tmin": np.round(tmin, 2),
        "rh": np.round(rh, 1), "u2": np.round(u2, 2),
        "rs": np.round(rs, 2), "rain": np.round(rain, 2),
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force-geojson", action="store_true",
                        help="即使 config/study_area.geojson 已存在也覆盖")
    args = parser.parse_args()

    raw = PROJECT_ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(args.seed)
    ts, truth = build_timeseries(rng)
    ts.to_csv(raw / "plot_timeseries.csv", index=False, encoding="utf-8-sig")
    truth.to_csv(raw / "ground_truth.csv", index=False, encoding="utf-8-sig")
    build_weather().to_csv(raw / "weather_synthetic.csv", index=False, encoding="utf-8-sig")

    gj_path = PROJECT_ROOT / "config" / "study_area.geojson"
    if gj_path.exists() and not args.force_geojson:
        print("保留已存在的 " + str(gj_path) + "（要覆盖请加 --force-geojson）")
    else:
        with open(gj_path, "w", encoding="utf-8") as fh:
            json.dump(build_geometry(), fh, ensure_ascii=False, indent=2)
        print("写出 " + str(gj_path) + " : " + str(len(PLOTS)) + " 个合成地块")

    print("写出 " + str(raw / "plot_timeseries.csv") + " : " + str(len(ts)) + " 行")
    print("写出 " + str(raw / "ground_truth.csv") + " : " + str(len(truth)) + " 行")
    print("写出 " + str(raw / "weather_synthetic.csv") + " : " +
          str(len(build_weather())) + " 天")
    print("地块 " + str(len(PLOTS)) + " 个 | 年份 " + str(YEARS))


if __name__ == "__main__":
    main()
