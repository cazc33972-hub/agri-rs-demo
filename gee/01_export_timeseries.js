/**
 * 01_export_timeseries.js
 * 导出「地块 x 日期」的 Sentinel-2 指数时间序列 -> CSV -> Google Drive
 *
 * 在 GEE Code Editor 中运行。产出 Drive/agri_rs/plot_timeseries.csv
 */

// ============================ 配置 ============================
var CFG = {
  studyAreaAsset: null,          // 例: 'projects/xxx/assets/study_area'；null 则用下面的手绘几何
  start: '2022-01-01',
  end:   '2024-12-31',
  cloudProbThreshold: 40,        // s2cloudless 云概率阈值
  maxCloudCover: 60,             // 场景级云量预筛
  indices: ['NDVI', 'EVI', 'SAVI', 'NDRE', 'NDWI'],
  scale: 10,
  driveFolder: 'agri_rs',
  filePrefix: 'plot_timeseries'
};

// ========================= 研究区 =========================
// TODO: 换成你自己的研究区
var roi = CFG.studyAreaAsset
  ? ee.FeatureCollection(CFG.studyAreaAsset)
  : ee.FeatureCollection([
      ee.Feature(ee.Geometry.Rectangle([116.1000, 36.3000, 116.1040, 36.3030]),
                 {plot_id: 'P001'}),
      ee.Feature(ee.Geometry.Rectangle([116.1050, 36.3000, 116.1095, 36.3032]),
                 {plot_id: 'P002'})
    ]);

// 确保每个 feature 都有 plot_id（手绘几何时用 system:index 兜底）
var ensurePlotId = function (f) {
  return ee.Feature(f).set('plot_id',
    ee.Algorithms.If(ee.Feature(f).get('plot_id'), ee.Feature(f).get('plot_id'),
                     ee.Feature(f).get('system:index')));
};
var plots = roi.map(ensurePlotId);

// ====================== 数据与云掩膜 ======================
var s2 = ee.ImageCollection(CFG.collection || 'COPERNICUS/S2_SR_HARMONIZED')
  .filterBounds(plots)
  .filterDate(CFG.start, CFG.end)
  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', CFG.maxCloudCover));

var s2Cloud = ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY')
  .filterBounds(plots)
  .filterDate(CFG.start, CFG.end);

// 按 system:index 把云概率影像拼到 SR 影像上
var joined = ee.Join.saveFirst('cloud_prob').apply({
  primary: s2,
  secondary: s2Cloud,
  condition: ee.Filter.equals({leftField: 'system:index', rightField: 'system:index'})
}).filter(ee.Filter.notNull(['cloud_prob']));   // 丢掉没有云概率影像的景，避免 get() 得到 null

/**
 * 云/云影/卷云掩膜。
 * 三路证据取并集，任一判定为污染就掩掉：
 *   1) s2cloudless 概率 > 阈值
 *   2) QA60 的卷云位(11)与不透明云位(10)
 *   3) SCL 场景分类：3=云影 8=中概率云 9=高概率云 10=薄卷云 11=雪
 * 注意：掩膜要在除以 10000 之前算，因为 QA60/SCL 是整数编码。
 */
function maskAndScale(img) {
  var cpImage = ee.Image(img.get('cloud_prob'));
  var isCloud = cpImage.select('probability').gte(CFG.cloudProbThreshold);

  var qa = img.select('QA60');
  var isOpaque = qa.bitwiseAnd(1 << 10).neq(0);
  var isCirrus = qa.bitwiseAnd(1 << 11).neq(0);

  var scl = img.select('SCL');
  var isBadScl = scl.eq(3).or(scl.eq(8)).or(scl.eq(9)).or(scl.eq(10)).or(scl.eq(11));

  var bad = isCloud.or(isOpaque).or(isCirrus).or(isBadScl);

  return img.updateMask(bad.not())
            .divide(10000)                                   // 反射率缩放
            .copyProperties(img, img.propertyNames());
}

/** 计算 5 个植被指数（输入已是 0~1 反射率） */
function addIndices(img) {
  var blue  = img.select('B2');
  var green = img.select('B3');
  var red   = img.select('B4');
  var re1   = img.select('B5');   // 红边 705nm
  var nir   = img.select('B8');
  var nirA  = img.select('B8A');

  var ndvi = nir.subtract(red).divide(nir.add(red)).rename('NDVI');
  var evi  = nir.subtract(red).multiply(2.5)
               .divide(nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1))
               .rename('EVI');                                  // 抗高生物量饱和
  var savi = nir.subtract(red).multiply(1.5)
               .divide(nir.add(red).add(0.5)).rename('SAVI');    // 土壤背景校正
  var ndre = nirA.subtract(re1).divide(nirA.add(re1)).rename('NDRE'); // 红边，比 NDVI 敏感
  var ndwi = green.subtract(nir).divide(green.add(nir)).rename('NDWI'); // 冠层水分

  return img.addBands([ndvi, evi, savi, ndre, ndwi]);
}

var processed = ee.ImageCollection(joined.map(function (f) {
  return maskAndScale(ee.Image(f));
})).map(addIndices);

// =============== 逐景 reduceRegions，保留时相 ===============
// 同时统计有效像元数，用于后续 QC 过滤
var reducer = ee.Reducer.mean().combine({
  reducer2: ee.Reducer.count(),
  sharedInputs: true
});

var perScene = processed.map(function (img) {
  var stats = img.select(CFG.indices).reduceRegions({
    collection: plots,
    reducer: reducer,
    scale: CFG.scale,
    tileScale: 4,
    bestEffort: true
  });
  var dateStr = img.date().format('YYYY-MM-dd');
  return stats.map(function (f) {
    var feat = ee.Feature(f);
    // 组合 reducer 的输出名是 <band>_mean / <band>_count，这里改回干净的 <band>
    CFG.indices.forEach(function (name) {
      feat = feat.set(name, feat.get(name + '_mean'));
    });
    // 每景覆盖的地块面积（m^2），用于算 valid_frac
    var area = feat.geometry().area(1);
    var nPix = ee.Number(feat.get('NDVI_count'));
    return feat
      .set('date', dateStr)
      .set('n_pixels', nPix)
      .set('valid_frac', nPix.multiply(CFG.scale * CFG.scale).divide(area).min(1));
  });
}).flatten();

// ========================== 导出 ==========================
var selectors = ['plot_id', 'date', 'n_pixels', 'valid_frac'].concat(CFG.indices);

Export.table.toDrive({
  collection: perScene,
  description: CFG.filePrefix,
  folder: CFG.driveFolder,
  fileNamePrefix: CFG.filePrefix,
  fileFormat: 'CSV',
  selectors: selectors
});

print('地块数量', plots.size());
print('可用影像数', processed.size());
print('样例', perScene.limit(5));

Map.centerObject(plots, 14);
Map.addLayer(processed.first().select(['NDVI']), {min: 0, max: 0.9,
  palette: ['#a50026', '#fdae61', '#ffffbf', '#a6d96a', '#1a9850']}, 'NDVI 首景');
Map.addLayer(plots.style({color: 'yellow', fillColor: '00000000', width: 2}), {}, '地块');
