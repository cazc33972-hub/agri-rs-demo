# GEE 脚本使用说明

这两个脚本在 **Google Earth Engine Code Editor**（https://code.earthengine.google.com）里运行，
结果导出到 Google Drive，再下载到 `data/raw/`。免费额度对 demo 规模完全够用。

## 先准备地块矢量

两种方式，二选一：

1. **上传 Asset（推荐，可复现）**
   - Assets → NEW → Shapefile/GeoJSON 上传
   - **必须包含一个 `plot_id` 字段**（字符串，如 P001）
   - 上传后得到形如 `projects/xxx/assets/study_area` 的 ID
   - 填进脚本顶部的 `CFG.studyAreaAsset`

2. **Code Editor 手绘**
   - 用矩形/多边形工具画在研究区上
   - 把脚本里 `ee.Geometry.Rectangle(...)` 一行换成你自己的几何
   - 手绘方式没有 plot_id，脚本会自动用 `system:index` 兜底（见 `ensurePlotId`）

## 跑 01_export_timeseries.js

产出：`Drive/agri_rs/plot_timeseries.csv`
一张长表，每行是「一个地块 × 一个成像日期」：

| plot_id | date | NDVI | EVI | SAVI | NDRE | NDWI | n_pixels | valid_frac |
|---|---|---|---|---|---|---|---|---|

- `n_pixels` / `valid_frac` 是 QC 字段：某景云太多时有效像元会很少，后面建模要按 `valid_frac` 过滤
- 下载后放到 `data/raw/plot_timeseries.csv`

**注意**：GEE 的 `Export.table.toDrive` 是异步任务，点 Run 之后要去 **Tasks** 面板点 Run，
跑完在 Drive 里下载。任务多了会被限流，耐心等。

## 跑 02_export_rasters.js

产出：`Drive/agri_rs/ndvi_decadal_YYYYMMDD.tif`
旬合成（每 10 天）NDVI 栅格，用于地图底图/出图。
demo 演示只要最近 1~2 个生长季就够，别导全时间序列（会跑很久）。

## 常见报错

| 报错 | 原因 | 处理 |
|---|---|---|
| `User memory limit exceeded` | 一次性 reduceRegions 的影像太多 | 拆成按年循环，或调大 `tileScale` |
| `Computation timed out` | 同上 | 缩小时间范围或研究区 |
| 导出的 CSV 里 NDVI 全是空的 | 云掩膜太狠 | 把 `cloudProbThreshold` 从 40 调到 60 |
| `Element.copyProperties` 相关报错 | 掩膜后丢了属性 | 脚本里已经用 copyProperties 处理，若改动注意保留 |
