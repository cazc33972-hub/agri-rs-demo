import time, urllib.request, urllib.error, re

URL = "https://cazc33972-hub.github.io/agri-rs-demo/"

for attempt in range(1, 13):
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", errors="replace")
            print("HTTP", r.status, "|", len(body), "bytes |", r.headers.get("Content-Type"))
            print("服务器:", r.headers.get("Server"))
            print("")
            title = re.search(r"<title>([^<]*)</title>", body)
            print("页面标题 :", title.group(1) if title else "(无)")
            print("含地块数据:", "AGRI_DATA" in body)
            print("含 Leaflet :", "leaflet" in body.lower())
            print("含 Chart.js:", "Chart" in body)
            n = len(re.findall(r'"plot_id"', body))
            print("地块记录数:", n)
            print("")
            print("线上地址可访问:", URL)
            break
    except urllib.error.HTTPError as e:
        print("尝试 %d: HTTP %s" % (attempt, e.code))
    except Exception as e:
        print("尝试 %d: %s" % (attempt, type(e).__name__))
    time.sleep(15)
else:
    print("多次尝试仍未就绪；Pages 首次发布有时需要几分钟")
