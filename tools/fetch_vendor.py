"""把前端依赖（Leaflet / Chart.js）抓到本地，供静态导出时内联。

为什么要内联：静态页如果靠 CDN 加载 Leaflet/Chart.js，一旦 CDN 在国内被卡，
页面就是白屏——而"随时能看"恰恰是这次的目标。内联之后整个页面只有一个外部依赖：
地图瓦片。瓦片挂了也不影响地块着色、曲线和处方。

用 npmmirror（淘宝镜像）下载，国内很快。

用法:
    python tools/fetch_vendor.py
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "static" / "vendor"

MIRROR = "https://registry.npmmirror.com"
ASSETS = [
    ("leaflet", "1.9.4", "dist/leaflet.js", "leaflet.js"),
    ("leaflet", "1.9.4", "dist/leaflet.css", "leaflet.css"),
    ("leaflet", "1.9.4", "dist/images/layers.png", "layers.png"),
    ("leaflet", "1.9.4", "dist/images/layers-2x.png", "layers-2x.png"),
    ("chart.js", "4.4.1", "dist/chart.umd.js", "chart.umd.js"),
]


def url_for(pkg: str, version: str, path: str) -> str:
    return MIRROR + "/" + pkg + "/" + version + "/files/" + path


def fetch(url: str) -> bytes:
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    return resp.content


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    for pkg, version, path, name in ASSETS:
        target = OUT / name
        if target.exists() and target.stat().st_size > 0:
            print("skip  " + name + " (已存在)")
            continue
        url = url_for(pkg, version, path)
        try:
            data = fetch(url)
        except Exception as exc:
            print("FAIL  " + name + "  <- " + url)
            print("      " + repr(exc))
            return 1
        target.write_bytes(data)
        print("ok    {0:<18} {1:>8.1f} KB".format(name, len(data) / 1024.0))

    # 把 CSS 里的图片引用换成 data URI，否则内联后 layer 控件的图标会丢
    css_path = OUT / "leaflet.css"
    css = css_path.read_text(encoding="utf-8")

    def to_data_uri(match: re.Match) -> str:
        fname = Path(match.group(1)).name
        img = OUT / fname
        if not img.exists():
            return match.group(0)
        b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        mime = "image/png" if fname.endswith(".png") else "image/svg+xml"
        return "url(data:" + mime + ";base64," + b64 + ")"

    css_new, n = re.subn(r"url\(([^)]+)\)", to_data_uri, css)
    if n:
        css_path.write_text(css_new, encoding="utf-8")

    print("")
    print("CSS 内联图片 " + str(n) + " 处")
    print("依赖目录: " + str(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
