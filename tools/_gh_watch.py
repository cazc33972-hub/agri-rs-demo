import json, subprocess, time, urllib.request, urllib.error

REPO = "cazc33972-hub/agri-rs-demo"
API = "https://api.github.com"

def get_token():
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=60)
    for line in out.stdout.strip().splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]

def call(path, token):
    req = urllib.request.Request(API + path)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "agri-rs-demo-setup")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}

token = get_token()
deadline = time.time() + 480
last = None
while time.time() < deadline:
    st, runs = call("/repos/" + REPO + "/actions/runs?per_page=1", token)
    if st != 200 or not runs.get("workflow_runs"):
        print("查询失败", st); break
    run = runs["workflow_runs"][0]
    key = (run["status"], run["conclusion"])
    if key != last:
        print("  [%s] status=%s conclusion=%s" % (time.strftime("%H:%M:%S"), run["status"], run["conclusion"]))
        last = key
    if run["status"] == "completed":
        print("")
        print("最终结论:", run["conclusion"])
        print("详情:", run["html_url"])
        st2, jobs = call("/repos/" + REPO + "/actions/runs/%d/jobs" % run["id"], token)
        for jb in jobs.get("jobs", []):
            print("  job %-10s %-10s %s" % (jb["name"], jb["status"], jb["conclusion"]))
            for s in jb.get("steps", []):
                mark = {"success": "OK ", "failure": "FAIL", "skipped": "-- "}.get(s["conclusion"], ".. ")
                print("      %s %s" % (mark, s["name"]))
        break
    time.sleep(15)
else:
    print("等待超时，工作流仍在运行")
