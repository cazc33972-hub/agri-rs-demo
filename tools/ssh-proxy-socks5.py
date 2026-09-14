#!/usr/bin/env python3
"""SSH 的 ProxyCommand：通过本地 SOCKS5 代理连接 GitHub。

## 解决什么问题

SSH 协议不能走 HTTP 代理。如果你的机器必须经代理才能出网（公司网络、Clash 类工具），
`git@github.com:...` 这样的地址会直接超时。这个脚本实现 SOCKS5 CONNECT，
让 ssh 把 TCP 连接"隧道"出去。

## 为什么不用现成工具

`ncat` / `nc` / `connect` / `corkscrew` 都能干这事，但 Windows 默认都不带。
Python 基本人人都有，写一个十行核心逻辑的脚本最省事。

## 用法

由 ssh 自动调用，不用手动跑：

    ssh -o ProxyCommand="python ssh-proxy-socks5.py %h %p" git@github.com

写进 `~/.ssh/config` 更省事（注意路径要用**正斜杠**，见下面的坑）：

    Host github.com
        HostName github.com
        User git
        IdentityFile C:/Users/<你>/.ssh/id_ed25519
        IdentitiesOnly yes
        StrictHostKeyChecking accept-new
        ProxyCommand python C:/path/to/ssh-proxy-socks5.py %h %p

## 踩过的坑

**路径必须用正斜杠。** Git for Windows 用的是自带的 MSYS 版 ssh，它执行 ProxyCommand
时会走 `/bin/sh`，而 sh 把反斜杠当转义符——`C:\\Users\\...` 会被吃成 `C:Users...`，
报 `not found`。直接调 Windows 自带的 OpenSSH 却没事，所以这个坑很隐蔽：
手动 `ssh -T git@github.com` 能通，`git fetch` 却失败。

## 可用环境变量覆盖代理地址

    GITHUB_PROXY_HOST   默认 127.0.0.1
    GITHUB_PROXY_PORT   默认 7897（Clash 常见的 mixed-port）
"""

import os
import socket
import struct
import sys
import threading

PROXY_HOST = os.environ.get("GITHUB_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("GITHUB_PROXY_PORT", "7897"))


def socks5_connect(host: str, port: int) -> socket.socket:
    """建立 SOCKS5 CONNECT 隧道，返回已连通的 socket。"""
    s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=15)

    # 1) 握手：声明只支持"无认证"
    s.sendall(b"\x05\x01\x00")
    resp = s.recv(2)
    if len(resp) < 2 or resp[0] != 0x05:
        raise RuntimeError("代理不是 SOCKS5（响应 %r）" % resp)
    if resp[1] != 0x00:
        raise RuntimeError("代理要求认证，本脚本只支持无认证（method=%d）" % resp[1])

    # 2) CONNECT：用域名形式（ATYP=3），让代理解析 DNS，绕开本地 DNS 污染
    h = host.encode("idna")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))

    # 3) 读响应
    head = s.recv(4)
    if len(head) < 4:
        raise RuntimeError("代理响应不完整")
    if head[1] != 0x00:
        raise RuntimeError("CONNECT 被拒绝，SOCKS5 错误码 %d" % head[1])

    # 4) 吃掉 BND.ADDR / BND.PORT，否则会污染后续数据流
    atyp = head[3]
    if atyp == 0x01:
        s.recv(4)
    elif atyp == 0x03:
        n = s.recv(1)
        if n:
            s.recv(n[0])
    elif atyp == 0x04:
        s.recv(16)
    s.recv(2)
    return s


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write("usage: ssh-proxy-socks5.py <host> <port>\n")
        return 2

    host, port = sys.argv[1], int(sys.argv[2])
    try:
        sock = socks5_connect(host, port)
    except Exception as exc:
        sys.stderr.write("proxy connect failed: %s\n" % exc)
        return 1

    def pump_out() -> None:
        """socket -> stdout"""
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        except Exception:
            pass
        finally:
            # 连接结束立刻整体退出。否则主线程还阻塞在 stdin 上，ssh 会一直挂着。
            os._exit(0)

    threading.Thread(target=pump_out, daemon=True).start()

    try:
        while True:
            chunk = sys.stdin.buffer.read1(65536)
            if not chunk:
                break
            sock.sendall(chunk)
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
