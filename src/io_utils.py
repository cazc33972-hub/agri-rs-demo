"""数据读写与列名规范化。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT

DEFAULT_INDICES = ["NDVI", "EVI", "SAVI", "NDRE", "NDWI"]


def ensure_dirs(*paths: str | Path) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def _strip_reducer_suffix(columns) -> list[str]:
    """GEE 组合 reducer 会导出 NDVI_mean 这类列名，统一还原成 NDVI。"""
    out = []
    for c in columns:
        c = str(c).strip()
        if c.endswith("_mean"):
            c = c[: -len("_mean")]
        out.append(c)
    return out


def load_timeseries(path: str | Path) -> pd.DataFrame:
    """读 GEE 导出的地块时序长表，返回标准化后的 DataFrame。

    必需列: plot_id, date, 至少一个指数列。
    可选列: n_pixels, valid_frac（QC 用）。
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = _strip_reducer_suffix(df.columns)

    required = {"plot_id", "date"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("时序表缺少必需列: " + ", ".join(sorted(missing)))

    df["plot_id"] = df["plot_id"].astype(str)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])

    index_cols = [c for c in df.columns if c in DEFAULT_INDICES]
    if not index_cols:
        raise ValueError("时序表里找不到任何指数列，期望其中之一: " + ", ".join(DEFAULT_INDICES))

    for c in index_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 数值越界的观测直接判为无效（云掩膜残留会产生异常值）
    for c in index_cols:
        df.loc[(df[c] < -1.0) | (df[c] > 1.0), c] = pd.NA

    df = df.sort_values(["plot_id", "date"]).reset_index(drop=True)
    df.attrs["index_cols"] = index_cols
    return df


def filter_by_quality(df: pd.DataFrame, min_valid_frac: float = 0.3) -> pd.DataFrame:
    """按有效像元比例过滤掉云污染严重的观测。"""
    if "valid_frac" not in df.columns:
        return df
    vf = pd.to_numeric(df["valid_frac"], errors="coerce")
    return df[(vf.isna()) | (vf >= min_valid_frac)].copy()


def load_ground_truth(path: str | Path) -> pd.DataFrame:
    """读实测数据。期望列: plot_id, year, <target>。

    target 由 config 的 model.target 决定（yield / lai / moisture）。
    这里只做规范化，不做列名假设。
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]
    if "plot_id" not in df.columns or "year" not in df.columns:
        raise ValueError("实测数据必须包含 plot_id 与 year 两列")
    df["plot_id"] = df["plot_id"].astype(str)
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    return df.dropna(subset=["year"]).reset_index(drop=True)


def save_table(df: pd.DataFrame, path: str | Path, float_format: str = "%.6f") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8-sig", float_format=float_format)
    return p
