"""特征组装：把时序曲线压成「地块-年」一行的建模特征表。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Cfg
from .phenology import doy_relative, extract_phenology, sos_doy
from .timeseries import fit_double_logistic, smooth_savgol, to_regular_grid

INDEX_COLS = ["NDVI", "EVI", "SAVI", "NDRE", "NDWI"]


def _season_stats(dates, values, step_days: int) -> dict:
    """一季的指数统计量：峰值、生长季均值、最小值和峰值日期。"""
    v = pd.Series(np.asarray(values, dtype=float))
    d = pd.to_datetime(pd.Series(list(dates))).reset_index(drop=True)
    ok = v.notna()
    if ok.sum() == 0:
        return {"max": np.nan, "mean": np.nan, "min": np.nan, "argmax_doy": np.nan}
    vv = v[ok]
    dd = d[ok].reset_index(drop=True)
    i = int(vv.to_numpy().argmax())
    return {
        "max": float(vv.max()),
        "mean": float(vv.mean()),
        "min": float(vv.min()),
        "argmax_doy": float(pd.Timestamp(dd.iloc[i]).dayofyear),
    }


def build_feature_table(
    ts: pd.DataFrame,
    cfg: Cfg,
    index_col: str = "NDVI",
    with_fit: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (特征表, 物候表)。

    特征表：一行 = 一个 地块-年，列 = 各指数的统计量 + NDVI 物候参数。
    物候表：一行 = 一个 地块-年，只放物候字段，便于单独出图。
    """
    step = int(cfg.get("timeseries.target_step_days", 5))
    gap = int(cfg.get("timeseries.interp_max_gap_days", 30))
    win = int(cfg.get("timeseries.savgol_window", 7))
    order = int(cfg.get("timeseries.savgol_polyorder", 2))
    min_obs = int(cfg.get("timeseries.min_obs_per_year", 8))
    sos_r = float(cfg.get("phenology.sos_threshold_ratio", 0.2))
    eos_r = float(cfg.get("phenology.eos_threshold_ratio", 0.2))
    min_amp = float(cfg.get("phenology.min_amplitude", 0.15))

    available = [c for c in INDEX_COLS if c in ts.columns]
    if index_col not in available:
        raise ValueError("时序表里没有 " + index_col + " 列")

    feat_rows: list[dict] = []
    pheno_rows: list[dict] = []

    # 按「生长年」而不是日历年分组：
    # 冬小麦 10 月播种、次年 6 月收获，日历年切分会把一个生长季拦腰截断，
    # 变成两个半年，物候参数完全失真。
    # 这里用标准农业年定义：9 月 1 日之后的数据归入下一个生长年。
    ts = ts.copy()
    ts["grow_year"] = ts["date"].dt.year + (ts["date"].dt.month >= 9).astype(int)

    for (plot_id, year), grp in ts.groupby(["plot_id", "grow_year"], sort=True):
        n_obs = int(grp[index_col].notna().sum())
        if n_obs < min_obs:
            continue

        row: dict = {"plot_id": plot_id, "year": int(year), "n_obs": n_obs}

        for col in available:
            grid = to_regular_grid(grp, value_col=col, step_days=step,
                                   max_gap_days=gap)
            if grid.empty:
                row[col + "_max"] = np.nan
                row[col + "_mean"] = np.nan
                row[col + "_min"] = np.nan
                row[col + "_argmax_doy"] = np.nan
                continue
            smooth = smooth_savgol(grid[col].to_numpy(), win, order)
            stats = _season_stats(grid["date"], smooth, step)
            row[col + "_max"] = stats["max"]
            row[col + "_mean"] = stats["mean"]
            row[col + "_min"] = stats["min"]
            row[col + "_argmax_doy"] = stats["argmax_doy"]

            if col == index_col:
                pheno = extract_phenology(
                    grid["date"], smooth, step_days=step,
                    sos_ratio=sos_r, eos_ratio=eos_r, min_amplitude=min_amp,
                )
                for k, val in pheno.items():
                    row["PHENO_" + k] = val
                row["PHENO_SOS_doy"] = sos_doy(pheno, int(year))
                row["PHENO_EOS_doy"] = doy_relative(pheno.get("EOS"), int(year))

                if with_fit:
                    fit = fit_double_logistic(grid["date"], smooth)
                    row["FIT_rmse"] = np.nan if fit is None else fit.rmse

                pheno_rows.append({
                    "plot_id": plot_id,
                    "year": int(year),
                    "SOS": pheno.get("SOS"),
                    "POS": pheno.get("POS"),
                    "EOS": pheno.get("EOS"),
                    "SOS_doy": row["PHENO_SOS_doy"],
                    "EOS_doy": row["PHENO_EOS_doy"],
                    "LOS": pheno.get("LOS"),
                    "NDVI_min": pheno.get("NDVI_min"),
                    "NDVI_max": pheno.get("NDVI_max"),
                    "NDVI_integral": pheno.get("NDVI_integral"),
                    "is_crop": pheno.get("is_crop"),
                })

        feat_rows.append(row)

    features = pd.DataFrame(feat_rows)
    phenology = pd.DataFrame(pheno_rows)
    return features, phenology


def feature_columns(features: pd.DataFrame, exclude=("plot_id", "year")) -> list[str]:
    """挑出真正参与建模的数值列。"""
    cols = []
    for c in features.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(features[c]) and features[c].notna().any():
            cols.append(c)
    return cols
