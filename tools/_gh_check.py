import json, subprocess, urllib.request, urllib.error

REPO = "cazc33972-hub/agri-rs-demo"
API = "https://api.github.com"

def get_token():
    inp = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run(["git", "credential", "fill"], input=inp,
                         capture_output=True, text=True, cwd=".", timeout=60)
    creds = {}
    for line in out.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            creds[k] = v
    return creds.get("password")

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
            return e.code, {"raw": raw[:200]}

token = get_token()
print("取得凭据:", "成功 (长度 %d)" % len(token) if token else "失败")
if not token:
    raise SystemExit(1)

st, repo = call("GET", "/repos/" + REPO, token)
print("仓库:", st, repo.get("full_name"), "| private =", repo.get("private"),
      "| default =", repo.get("default_branch"))

st, pages = call("GET", "/repos/" + REPO + "/pages", token)
print("Pages 状态:", st, "(404 表示尚未启用)")
if st == 200:
    print("  当前配置:", {k: pages.get(k) for k in ("build_type", "html_url", "status")})

st, runs = call("GET", "/repos/" + REPO + "/actions/runs?per_page=3", token)
print("Actions 运行:", st)
for r in runs.get("workflow_runs", [])[:3]:
    print("  #%s %s | %s | %s" % (r["run_number"], r["name"], r["status"], r["conclusion"]))
    print("     ", r["html_url"])
