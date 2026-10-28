#!/usr/bin/env python3
"""阶段 B：真实 Chromium 双 BrowserContext / HTTP-SOCKS5 / Cookie 隔离验证。

仅连接公开 Cloudflare trace，不访问 Muse、不使用账号或 Cookie。
此脚本需要本机 Chromium/Chrome；不会在 CI 中启动浏览器。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time

from config import Config
from engine import MuseEngine
from proxy_bridge import _Handler, parse_proxy

TRACE = "https://www.cloudflare.com/cdn-cgi/trace"
COOKIE_URL = "https://example.com/"
COOKIE_NAME = "muse2api_stage_b_isolation"


class CountingProxyHandler(_Handler):
    def handle(self):
        with self.server.hit_lock:
            self.server.hit_count += 1
        super().handle()


def browser_binary(explicit: str) -> str:
    options = [
        explicit,
        os.environ.get("MUSE2API_CHROMIUM", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ]
    for option in options:
        if option and (Path(option).is_file() or shutil.which(option)):
            return option
    raise RuntimeError("找不到 Chromium/Chrome；请使用 --browser 指定应用内的可执行文件")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def await_trace(page, seconds=30) -> str:
    expiry = time.monotonic() + seconds
    while time.monotonic() < expiry:
        body = page.js("document.body ? document.body.innerText : ''")
        if isinstance(body, str) and "ip=" in body and "h=" in body:
            return body
        time.sleep(0.3)
    raise RuntimeError("没有在 Chromium 页面中取得 Cloudflare trace，请检查代理规则或加载情况")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="", help="Chromium/Chrome 可执行文件路径")
    parser.add_argument("--http", default="http://127.0.0.1:20240")
    parser.add_argument("--socks5", default="socks5://127.0.0.1:65220")
    args = parser.parse_args()
    paths = {"HTTP": args.http, "SOCKS5": args.socks5}
    for label, value in paths.items():
        spec = parse_proxy(value)
        if spec.username or spec.password:
            raise ValueError("本测试只接受无认证本地代理地址，禁止在命令行传递凭据")
        if spec.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("阶段 B 仅接受本地回环代理")
        if (label == "HTTP" and spec.scheme != "http") or (
                label == "SOCKS5" and spec.scheme != "socks5"):
            raise ValueError("HTTP 与 SOCKS5 端口协议填写不匹配")

    chrome = browser_binary(args.browser)
    print("Chromium:", chrome)
    # CDP 只能由本机直连；系统代理不得拦截 loopback。
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    with tempfile.TemporaryDirectory(prefix="muse2api-stage-b-") as home:
        cfg = Config(base_dir=home, profile_root=str(Path(home) / "profiles"),
                     chromium=chrome, cdp_port=free_port(), extra_path="")
        cfg.ensure_dirs()
        engine = MuseEngine(cfg)
        barrier = threading.Barrier(2, timeout=20)
        local = {}
        servers = {}
        try:
            for label, address in paths.items():
                local[label] = engine.proxy_bridges.local_url(label, address)
                server = engine.proxy_bridges._entries[label][1]
                server.hit_lock = threading.Lock()
                server.hit_count = 0
                server.RequestHandlerClass = CountingProxyHandler
                servers[label] = server

            engine.start()  # 真正启动隔离的 Chromium；不打开 Muse。
            print("CDP:", f"127.0.0.1:{cfg.cdp_port}")

            def test_context(label: str):
                page = None
                try:
                    page = engine._open_page(proxy_server=local[label])
                    engine.page = page
                    context_id = engine.current_context_id
                    token = secrets.token_hex(16)
                    result = page.send("Network.setCookie", {
                        "url": COOKIE_URL, "name": COOKIE_NAME,
                        "value": token, "secure": True,
                    })
                    if result.get("result", {}).get("success") is not True:
                        raise RuntimeError("Network.setCookie 未确认成功")
                    barrier.wait()
                    cookie_response = page.send("Network.getCookies", {
                        "urls": [COOKIE_URL],
                    })
                    values = [
                        c.get("value") for c in cookie_response.get("result", {}).get("cookies", [])
                        if c.get("name") == COOKIE_NAME
                    ]
                    if values != [token]:
                        raise RuntimeError("Cookie 互相泄漏或没有读取到独立 Cookie")
                    navigation = page.send("Page.navigate", {"url": TRACE}, timeout=20)
                    error_text = navigation.get("result", {}).get("errorText")
                    if error_text:
                        raise RuntimeError("Page.navigate 失败: " + error_text)
                    trace = await_trace(page)
                    ip = next((line[3:] for line in trace.splitlines()
                               if line.startswith("ip=")), "")
                    if not ip:
                        raise RuntimeError("未找到 Cloudflare 出口 IP")
                    return label, context_id, ip
                finally:
                    if page is not None:
                        engine.close_current_page()

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(test_context, label) for label in paths]
                results = [future.result(timeout=70) for future in futures]

            contexts = [result[1] for result in results]
            if len(set(contexts)) != 2:
                raise RuntimeError("两个账号使用了相同的 BrowserContext ID")
            for label in paths:
                if servers[label].hit_count < 1:
                    raise RuntimeError(label + " 的本地代理桥没有收到 Chromium 请求")
            if engine._active_contexts:
                raise RuntimeError("BrowserContext 清理后仍有活跃 ID")

            for label, _, ip in results:
                print(f"{label}: PASS | 出口 IP: {ip} | 本地代理连接次数: {servers[label].hit_count}")
            print("BrowserContext ID 不同: PASS")
            print("双 Context Cookie 隔离: PASS")
            print("独立代理桥转发: PASS")
            print("Context 资源清理: PASS")
            print("阶段 B: PASS（两个公网出口可以相同）")
        finally:
            engine.stop()
            print("测试 Chromium 和代理桥已关闭；临时 profile 将删除")


if __name__ == "__main__":
    main()
