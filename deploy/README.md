# 上线部署：把 demo 变成随时能打开的网址

## 先搞清楚一件事

这个 demo 有**两种形态**，选错了会白折腾：

| 形态 | 是什么 | 适合 |
|---|---|---|
| **静态页**（推荐） | `dist/index.html`，713 KB 单文件，数据全部内联，两个前端库也内联了，**零后端** | 给人看、当作品集、发给面试官/导师 |
| **在线服务** | FastAPI 常驻进程，暴露 `/api/*` | 需要实时重新计算、或要看起来"像个系统" |

关键点：**你的数据是批处理产物**——跑一次管线，结果就固定了。
这种情况下静态页能提供完全一样的交互，却不需要一台一直开着的机器、不需要运维、不花钱。
先把静态页发出去，真有实时需求再上服务器。

---

## 方案 A：静态页 + 免费托管（推荐）

### 生成本地静态页

```bash
python -m src.pipeline          # 先跑管线出结果
python -m src.export_static     # 导出 dist/index.html
```

产物是**一个文件**，双击就能打开，不依赖任何服务器。可以先本地自查渲染是否正常：

```bash
python tools/check_static_page.py     # 用无头 Chrome 真渲染一遍，检查关键内容
```

### A1. GitHub Pages（最省事）

```bash
git init
git add .
git commit -m "agri rs demo"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

推上去之后，在 GitHub 仓库里做**一步**设置：

> **Settings → Pages → Build and deployment → Source 选 "GitHub Actions"**

工作流已经在 `.github/workflows/pages.yml`，不需要再复制任何东西。
之后每次 push 都会自动重跑管线 + 重新导出 + 发布，也支持手动触发和每周定时重建。
地址形如 `https://<用户名>.github.io/<仓库名>/`。

> 顺序建议：**先在 Settings 里把 Pages 打开，再 push**。
> 反过来的话，build 环节会正常通过，只有 deploy 环节报"Pages 未启用"，看着像出错其实不是。

> **国内访问**：GitHub Pages 时有波动。如果打开慢，在 Cloudflare 上给这个域名加一层代理
> （需要一个自己的域名，免费套餐够用），或者直接用下面的 A3。

### A2. Cloudflare Pages

1. 把仓库推到 GitHub
2. Cloudflare Dashboard → Workers & Pages → Create → Pages → 连接仓库
3. 构建设置：
   - Build command: `pip install -r requirements.txt && python -m src.pipeline && python -m src.export_static --out dist`
   - Build output directory: `dist`
4. 保存并部署

免费额度对 demo 绰绰有余，自带全球 CDN。

### A3. 国内云服务器（国内访问最快，也最稳）

静态页丢到 Nginx 就行，**用 IP + 端口访问不需要备案**（绑定自有域名才需要）。

```bash
# 本地：把 dist 打包
tar -czf dist.tar.gz dist

# 服务器上
scp dist.tar.gz root@<你的服务器IP>:/var/www/
ssh root@<你的服务器IP>
cd /var/www && tar -xzf dist.tar.gz
```

已经给了现成的 `deploy/nginx.conf`（含 gzip、缓存、SPA 兜底）：

```bash
cp deploy/nginx.conf /etc/nginx/conf.d/agri.conf
nginx -t && systemctl reload nginx
```

或者直接用容器（`deploy/Dockerfile` 是 nginx 版静态镜像）：

```bash
docker build -f deploy/Dockerfile -t agri-rs-demo .
docker run -d --name agri-rs -p 8080:80 --restart unless-stopped agri-rs-demo
```

然后访问 `http://<你的服务器IP>:8080`。

学生机价格通常在几十到一百多一年；不需要域名和备案，这是国内最省心的路径。

### A4. 对象存储静态托管（阿里云 OSS / 腾讯云 COS）

便宜、国内快，但有一个**必须知道的坑**：
用厂商的**默认域名**访问 HTML，会被强制以附件形式下载而不是在浏览器里打开。
要正常预览，得绑定**已完成备案的自有域名**。

如果你已经有备案域名，这条路很划算；没有的话建议先走 A3。

---

## 方案 B：云服务器跑 FastAPI（需要后端时才用）

保留 `/api/*` 接口，适合以后要接实时数据、或者导师明确要求"系统"形态。

```bash
# 服务器上
git clone <你的仓库> && cd agri-rs-demo
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m src.pipeline
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 &
```

建议用 `deploy/nginx.conf` 里的反向代理段，把 80 端口转到 8000。

---

## 几个常见问题的答案

**Q: 页面打开是白的？**
A: 先本地用 `python tools/check_static_page.py` 确认真能渲染。线上白屏通常是两种情况：
文件没传全（`dist/index.html` 是单文件，但如果你用了 A2 的构建，确认输出目录是 `dist`），
或者托管平台对 `.html` 做了下载处理（见 A4 的坑）。

**Q: 地图底图是灰的？**
A: 地图瓦片来自 Esri / CARTO / OSM，是页面唯一的外部依赖。国内访问 Esri 时快时慢，
页面左上角有图层切换，可以换成 CARTO 深色底图或 OSM。
**瓦片挂了不影响地块着色、曲线和灌溉处方**——那些数据都内联在页面里。

**Q: 数据更新了怎么重新发布？**
A: 静态页是快照。重跑 `python -m src.export_static` 再推一次即可；
用 A1 的 GitHub Actions 的话，push 就自动完成。

**Q: 能不能做到每天自动更新？**
A: 能。`.github/workflows/pages.yml` 里已经带了定时触发（默认每周一），
改成 `cron: '0 2 * * *'` 就是每天凌晨 2 点重建。
注意 GEE 那边的影像导出仍需手动或另配服务账号 —— 自动化的只是"拿到数据之后的整条链路"。

---

## 附：受限网络下怎么推代码

如果机器必须经代理才能出网（公司网络、Clash 类工具），`git push` 会遇到两个坑。

### 坑 1：git 不读系统代理设置

Windows 的"系统代理"是 WinINET 的设置，**git 不走它**，会直连并超时。要给 git 单独配：

```bash
# 只对本仓库生效（推荐，不动全局配置）
git config --local http.proxy "http://127.0.0.1:7897"
git config --local https.proxy "http://127.0.0.1:7897"
```

### 坑 2：SSH 协议穿不过 HTTP 代理

`git@github.com:...` 这种地址用的是 SSH，而 SSH 不是 HTTP，
**代理配置对它无效**。有两条路：

**方案一：直接用 HTTPS**（最省事）

```bash
git remote set-url origin https://github.com/<用户名>/<仓库名>.git
```

首次推送会触发 Git Credential Manager 授权。
如果它弹的授权页打不开（比如默认浏览器有问题），改用设备码流程：

```bash
# 只对这次命令生效
GCM_GITHUB_AUTHMODES=device git push -u origin main
# 终端/GUI 会给一个形如 XXXX-XXXX 的码，去 https://github.com/login/device 输入
```

**方案二：给 SSH 配代理隧道**

用仓库里的 `tools/ssh-proxy-socks5.py` 充当 `ProxyCommand`：

```
# ~/.ssh/config
Host github.com
    HostName github.com
    User git
    IdentityFile C:/Users/<你>/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    ProxyCommand python C:/path/to/ssh-proxy-socks5.py %h %p
```

> **路径必须用正斜杠。** Git for Windows 用的是自带的 MSYS 版 ssh，
> 它执行 ProxyCommand 时走 `/bin/sh`，而 sh 把反斜杠当转义符——
> `C:\Users\...` 会被吃成 `C:Users...` 并报 `not found`。
> 这个坑很隐蔽：手动跑 `ssh -T git@github.com` 能通（用的是 Windows 自带 OpenSSH），
> 但 `git fetch` 失败。

怎么判断隧道是否正常：跑 `ssh -T git@github.com`。
返回 `Permission denied (publickey)` 说明**隧道是通的**（已经连到 GitHub，只是密钥没注册）；
返回超时或 `ProxyCommand failed` 才是隧道本身有问题。
