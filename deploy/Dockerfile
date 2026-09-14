# 静态版镜像：构建阶段跑管线 + 导出，运行阶段只用 nginx 伺候一个 HTML。
# 这样线上不需要 Python 运行时，镜像小、启动快、不吃内存。

FROM python:3.12-slim AS build

WORKDIR /app

# 依赖单独一层，改代码不会触发重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 没放真实数据时先生成合成数据，保证镜像一定能构建成功
RUN if [ ! -f data/raw/plot_timeseries.csv ]; then \
        python tools/make_synthetic_data.py --force-geojson; \
    fi \
    && python -m src.pipeline \
    && python -m src.export_static --out dist \
    && test -f dist/index.html

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1
