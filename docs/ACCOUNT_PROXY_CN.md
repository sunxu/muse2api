# muse2api 多账号认证代理（P1）

> 开发分支功能。仅在明确配置的账号上启用代理；未配置账号保持原项目默认网络行为。未进行真实 Muse 账号的端到端验证。

## 配置

在管理后台的「账号列表」点击「设置代理」，或在导入账号时填写认证代理。支持：

```text
http://username:password@proxy.example.com:8080
socks5://username:password@proxy.example.com:1080
```

- 用户名、密码中的 `@`、`:`、`/`、`#`、`%` 等特殊字符需进行 URL 百分号编码。
- 不支持无认证代理、HTTPS 上游代理或 SOCKS4；这是当前最小版本的刻意限制。
- API 方式：`PATCH /admin/accounts/{id}`，JSON 为 `{"proxy_url":"socks5://user:pass@host:1080"}`。
- 清除：`PATCH /admin/accounts/{id}`，JSON 为 `{"proxy_url":""}`。
- 状态：`GET /admin/accounts` 返回 `has_proxy` 布尔值，**不会返回** `proxy_url`。
- 测试：`POST /admin/accounts/{id}/proxy-test`。该动作明确通过代理访问固定的公网出口 IP 检测服务，**不会访问 Muse**。仅在管理员触发时执行。

## 实现和边界

每个账号在进程内获得独立的 `127.0.0.1` HTTP 代理监听端口。Chromium CDP 为各账号的 BrowserContext 指定对应的本地代理；本地桥接模块为上游 HTTP Basic 或 SOCKS5 用户名密码代理进行认证。Python 的 `/api/session`、VM 唤醒及核验显式使用同一桥接出口。

认证错误、无法连接或代理故障直接失败，不改用直连；BrowserContext 创建失败也禁止回退普通标签页。原有账号并发控制仍然有效。

**请注意：**
- 代理密码存于本地 `data/accounts.json`，不是加密数据库；写入时限制文件权限为 `0600`，须确保运行机器与备份可信。该文件被 `.gitignore` 排除，严禁手动提交。
- 本地监听仅绑定 loopback，未额外设置监听端口密码。部署在共享机器时应使用操作系统或容器隔离，禁止对外转发本地代理端口。
- HTTP Basic 和 SOCKS5 用户名密码认证本身不保证到代理服务器链路加密。应使用可信代理服务或额外加密隧道。
- 改变代理绑定前需要账号处于空闲状态，否则返回 HTTP 409。
- 网页行为可能变化；本分支目前只通过离线代理模拟器和现有回归测试，未验证真实第三方代理、Chromium 网络流或 Muse 的连接表现。
- 独立代理仅用于网络隔离与可靠性，不提供规避 Muse 上游限制的功能。

## 离线测试

```bash
python -m pip install -r requirements.txt pytest httpx
python -m pytest -q tests/test_browser_context_isolation.py tests/test_proxy_bridge.py tests/test_concurrency.py
python tests/test_session_health.py
python tests/test_wake_confirmation.py
python tests/test_quota_account_binding.py
python tests/test_keepalive_scheduling.py
python tests/test_refusal_rotation.py
python tests/test_async_images.py
```

任何真实账号及出口测试均须单独批准，不属于离线 CI。
