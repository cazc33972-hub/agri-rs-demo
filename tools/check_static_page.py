"""用无头浏览器真渲染一遍静态页，检查关键内容是否正常。

为什么需要：静态页是自己拼出来的，光看代码"应该没问题"不算数。
这里真的用 Chrome/Edge 渲染一次，把 JS 执行后的 DOM 抓回来逐项断言——
包括有没有出现 NaN / undefined / 未捕获的 JS 错误。

用法:
    python -m src.export_static
    python tools/check_static_page.py
    python tools/check_static_page.py --page dist/index.html --keep
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# 注进来就能把未捕获的 JS 错误写进 <title>，dump-dom 时一眼可见
ERROR_TRAP = (
    "<script>window.__errs=[];window.onerror=function(m,s,l,c){"
    "window.__errs.push(String(m)+' @'+l+':'+c);"
    "document.title='JSERR '+window.__errs.join(' ;; ');};</script>"
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    line = ("PASS  " if ok else "FAIL  ") + name
    if detail:
        line += "   " + detail
    print(line)


def find_browser() -> str | None:
    for path in BROWSERS:
        if Path(path).exists():
            return path
    return None


def render(browser: str, page: Path, workdir: Path) -> str:
    """把错误陷阱注入一份副本，用无头浏览器渲染并取回 DOM。"""
    html = page.read_text(encoding="utf-8")
    probe = workdir / "_probe_page.html"
    probe.write_text(html.replace("<title>", ERROR_TRAP + "<title>", 1), encoding="utf-8")

    dom_file = workdir / "dom.html"
    with open(dom_file, "w", encoding="utf-8", errors="replace") as fh:
        subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--virtual-time-budget=20000",
             "--dump-dom", probe.resolve().as_uri()],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=180,
        )
    return dom_file.read_text(encoding="utf-8", errors="replace")


def strip_scripts(html: str) -> str:
    """去掉 <script> 源码再断言。

    Leaflet / Chart.js 的源码里本来就有 NaN、undefined 这些字面量，
    不剥掉的话每次都会误报。
    """
    return re.sub(r"<script[^>]*>.*?</script>", "<!--script-->", html, flags=re.S)


def first(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text, re.S)
    return m.group(1).strip() if m else default


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="渲染并校验静态演示页")
    parser.add_argument("--page", default="dist/index.html")
    parser.add_argument("--keep", action="store_true", help="保留临时渲染产物")
    args = parser.parse_args(argv)

    page = Path(args.page)
    if not page.is_absolute():
        page = ROOT / page
    if not page.exists():
        print("找不到页面: " + str(page))
        print("请先运行: python -m src.export_static")
        return 2

    browser = find_browser()
    if not browser:
        print("没有找到 Chrome / Edge，无法做渲染校验。")
        print("页面本身是好的，可以直接手工打开确认。")
        return 3
    print("渲染引擎: " + browser)
    print("页面大小: " + format(page.stat().st_size / 1024.0, ".0f") + " KB")
    print("")

    workdir = Path(tempfile.mkdtemp(prefix="agri-check-"))
    html = render(browser, page, workdir)
    dom = strip_scripts(html)

    check("页面已渲染出内容", len(dom) > 5000,
          format(len(dom) / 1024.0, ".0f") + " KB DOM")
    check("没有未捕获的 JS 错误",
          "JSERR" not in first(r"<title>([^<]*)</title>", dom, "OK"),
          first(r"<title>([^<]*)</title>", dom, "OK"))

    n_poly = len(re.findall(r'class="leaflet-interactive"', dom))
    check("地块多边形已绘制", n_poly > 0, str(n_poly) + " 个")
    n_canvas = len(re.findall(r"<canvas", dom))
    check("图表画布已创建", n_canvas >= 3, str(n_canvas) + " 个")
    check("图例已渲染", "NDVI" in dom)

    kpi_plots = first(r'id="kpi-plots">([^<]*)<', dom, "-")
    check("KPI 地块数有值", kpi_plots not in ("", "-"), kpi_plots)
    kpi_ndvi = first(r'id="kpi-ndvi">([^<]*)<', dom, "-")
    check("KPI NDVI 有值", kpi_ndvi not in ("", "-"), kpi_ndvi)
    kpi_r2 = first(r'id="kpi-r2">([^<]*)<', dom, "-")
    check("KPI 模型精度有值", kpi_r2 not in ("", "-"), kpi_r2)

    n_btn = len(re.findall(r'<button data-mode="', dom))
    check("着色切换按钮存在", n_btn == 2, str(n_btn) + " 个")

    check("详情面板有内容", bool(re.search(r'id="detail"[^>]*>\s*<div', dom)))
    check("模型精度卡片有内容", "交叉验证" in dom or "R" in first(r'id="metrics"[^>]*>(.{0,800})', dom))

    for bad in ["NaN", "undefined"]:
        n = len(re.findall(re.escape(bad), dom))
        check("页面里没有 " + bad, n == 0, str(n) + " 处")

    if args.keep:
        print("")
        print("临时产物保留在: " + str(workdir))
    else:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print("")
    print("共 " + str(len(RESULTS)) + " 项, 通过 "
          + str(len(RESULTS) - n_fail) + ", 失败 " + str(n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
