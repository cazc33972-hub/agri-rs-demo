import json, subprocess, time, urllib.request, urllib.error

REPO = "cazc33972-hub/agri-rs-demo"
API = "https://api.github.com"

def get_token():
    inp = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run(["git", "credential", "fill"], input=inp,
                         capture_output=True, text=True, timeout=60)
    for line in out.stdout.strip().splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return None

def call(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "agri-rs-demo-setup")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}

token = get_token()
st, res = call("POST", "/repos/" + REPO + "/pages", token, {"build_type": "workflow"})
print("启用 Pages:", st)
if st in (200, 201, 409):
    if st == 409:
        print("  (409 = 已经启用过了)")
else:
    print("  返回:", json.dumps(res, ensure_ascii=False)[:300])

time.sleep(2)
st, pages = call("GET", "/repos/" + REPO + "/pages", token)
print("复核 Pages:", st)
if st == 200:
    print("  build_type :", pages.get("build_type"))
    print("  html_url   :", pages.get("html_url"))
    print("  status     :", pages.get("status"))

st, runs = call("GET", "/repos/" + REPO + "/actions/runs?per_page=3", token)
print("")
print("Actions:")
for r in runs.get("workflow_runs", [])[:3]:
    print("  #%s %s | %s | %s" % (r["run_number"], r["name"], r["status"], r["conclusion"]))
