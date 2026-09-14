/**
 * 02_export_rasters.js
 * 导出旬合成 NDVI 栅格（GeoTIFF），用于地图底图和报告出图。
 *
 * 建议只导最近 1~2 个生长季，全时间序列会很慢。
 */

var CFG = {
  studyAreaAsset: null,
  start: '2024-03-01',
  end:   '2024-10-31',
  cloudProbThreshold: 40,
  maxCloudCover: 60,
  compositeDays: 10,      // 旬合成
  scale: 10,
  driveFolder: 'agri_rs',
  filePrefix: 'ndvi_decadal'
};

var roi = CFG.studyAreaAsset
  ? ee.FeatureCollection(CFG.studyAreaAsset)
  : ee.FeatureCollection([ee.Feature(ee.Geometry.Rectangle([116.09, 36.29, 116.12, 36.31]))]);

var bounds = roi.geometry().bounds();

var s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
  .filterBounds(bounds).filterDate(CFG.start, CFG.end)
  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', CFG.maxCloudCover));

var s2Cloud = ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY')
  .filterBounds(bounds).filterDate(CFG.start, CFG.end);

var joined = ee.Join.saveFirst('cloud_prob').apply({
  primary: s2, secondary: s2Cloud,
  condition: ee.Filter.equals({leftField: 'system:index', rightField: 'system:index'})
}).filter(ee.Filter.notNull(['cloud_prob']));

function maskAndScale(img) {
  var cp = ee.Image(img.get('cloud_prob')).select('probability');
  var bad = cp.gte(CFG.cloudProbThreshold)
    .or(img.select('QA60').bitwiseAnd(1 << 10).neq(0))
    .or(img.select('QA60').bitwiseAnd(1 << 11).neq(0))
    .or(img.select('SCL').eq(3)).or(img.select('SCL').eq(8))
    .or(img.select('SCL').eq(9)).or(img.select('SCL').eq(10))
    .or(img.select('SCL').eq(11));
  return img.updateMask(bad.not()).divide(10000)
            .copyProperties(img, img.propertyNames());
}

function addNdvi(img) {
  var nir = img.select('B8'), red = img.select('B4');
  return img.addBands(nir.subtract(red).divide(nir.add(red)).rename('NDVI'));
}

var col = ee.ImageCollection(joined.map(function (f) {
  return addNdvi(maskAndScale(ee.Image(f)));
}));

// ================= 按旬合成 =================
var startDate = ee.Date(CFG.start);
var endDate = ee.Date(CFG.end);
var nPeriods = endDate.difference(startDate, 'day').divide(CFG.compositeDays).ceil();

var decadal = ee.ImageCollection(
  ee.List.sequence(0, nPeriods.subtract(1)).map(function (i) {
    var t0 = startDate.advance(ee.Number(i).multiply(CFG.compositeDays), 'day');
    var t1 = t0.advance(CFG.compositeDays, 'day');
    var sub = col.filterDate(t0, t1);
    var img = ee.Image(ee.Algorithms.If(
      sub.size().gt(0),
      sub.select('NDVI').median().rename('NDVI'),
      ee.Image(0).rename('NDVI').updateMask(ee.Image(0))   // 无观测 -> 全掩膜
    ));
    return img.set('system:time_start', t0.millis())
              .set('date', t0.format('YYYYMMdd'));
  })
);

Export.image.toDrive({
  image: decadal.toBands(),
  description: CFG.filePrefix,
  folder: CFG.driveFolder,
  fileNamePrefix: CFG.filePrefix,
  region: bounds,
  scale: CFG.scale,
  crs: 'EPSG:32650',       // TODO 换成你研究区对应的 UTM 带号
  maxPixels: 1e10
});

print('旬合成期数', nPeriods);
print('有效影像数', col.size());
Map.centerObject(bounds, 13);
Map.addLayer(decadal.select('NDVI').max(), {min: 0, max: 0.9,
  palette: ['#a50026', '#fdae61', '#ffffbf', '#a6d96a', '#1a9850']}, 'NDVI 峰值合成');
