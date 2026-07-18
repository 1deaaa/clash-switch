# Clash Verge Rev 节点守护技术文档

本文档记录本项目使用的 Clash Verge Rev / Mihomo 控制方式、节点切换 API、配置参数和 Linux 部署注意事项。资料核对日期：2026-07-12。

## 1. 版本与控制方式

- 当前 Clash Verge Rev 最新发布线：`2.5.1`。
- 本机实测内置 Mihomo：`v1.19.25`。
- 节点读取、连通性探测和切换实际由 Mihomo REST API 完成。
- Clash Verge Rev 新版本默认使用本地 IPC 与 Mihomo 通信，外部 TCP 控制器默认可关闭。
- Windows 默认 IPC：`\\.\pipe\verge-mihomo`。
- Linux 常见 IPC：`/tmp/verge/verge-mihomo.sock`。
- 外部控制器示例：`http://127.0.0.1:9097`。只有在 Clash Verge Rev 中开启“外部控制”后才会监听。

本项目的连接优先级：

1. `config.json` 明确配置的 `controller_url`；失败后尝试本地 IPC。
2. Windows 使用命名管道 `\\.\pipe\verge-mihomo`。
3. Linux 使用配置中的 Unix Socket，或自动查找 `/tmp/verge/verge-mihomo.sock`。

> 注意：Mihomo REST API 是官方接口；Clash Verge Rev 的内部 IPC 路径是当前版本实现细节，官方讨论明确表示不会将内部 IPC 作为面向用户的稳定接口文档。若未来版本改变 IPC 路径，可开启仅监听 `127.0.0.1` 的外部控制器，并填写 `controller_url`。

## 2. 鉴权

Mihomo 配置中的控制密钥：

```yaml
secret: your-secret
```

HTTP 请求头：

```http
Authorization: Bearer your-secret
```

项目不会把密钥写入 `config.json`，而是从本机 Clash Verge Rev 的 `config.yaml` 读取。外部控制器不要监听公网地址；确需远程控制时应使用防火墙和 SSH 隧道。

## 3. 项目使用的 Mihomo API

### 3.1 查询版本

```http
GET /version
```

典型响应：

```json
{"meta": true, "version": "v1.19.25"}
```

### 3.2 查询运行配置

```http
GET /configs
```

可读取 `mode`、`mixed-port`、`tun.enable` 等实际运行参数。它反映内核当前状态，不应只依赖订阅源文件推断运行状态。

### 3.3 查询策略组与节点

```http
GET /proxies
```

响应主体的 `proxies` 是以名称为键的对象。项目重点使用以下字段：

| 字段 | 含义 |
| --- | --- |
| `type` | 节点或策略组类型，例如 `Selector`、`URLTest`、`VMess` |
| `now` | 策略组当前选中的节点或下级策略组 |
| `all` | 策略组包含的全部成员名称 |
| `alive` | Mihomo 当前记录的存活状态 |
| `history` | 历史延迟记录 |
| `provider-name` | 节点所属代理提供者；内联节点可能没有该字段 |

GUI 只把 `type=Selector` 的对象列为可切换策略组，并从其 `all` 数组提取真实节点。`Selector`、`URLTest`、`Fallback`、`LoadBalance`、`DIRECT` 和 `REJECT` 不作为最终候选节点。

### 3.4 测试指定节点访问目标网址

```http
GET /proxies/{节点名称}/delay?url={网址}&timeout={毫秒}
```

参数：

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `url` | 是 | 由该节点实际访问的 HTTP/HTTPS 网址 |
| `timeout` | 是 | 单个节点探测超时，单位毫秒 |
| `expected` | 否 | 期望状态码，例如 `200`、`200/204`、`200-299` |

成功响应：

```json
{"delay": 308}
```

节点名称和查询参数必须进行 URL 编码。该接口由 Mihomo 自己通过指定节点发起请求，不要求运行 Python 的进程走系统代理，也不要求开启 TUN。

### 3.5 切换 Selector 策略组

```http
PUT /proxies/{策略组名称}
Content-Type: application/json

{"name": "目标节点名称"}
```

成功返回 HTTP `204`，没有响应主体。切换后可再次调用 `GET /proxies`，确认该组的 `now` 已变为目标节点。

等价 HTTP 示例：

```bash
curl -X PUT \
  -H "Authorization: Bearer YOUR_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"name":"Japan"}' \
  "http://127.0.0.1:9097/proxies/Proxy"
```

组名和节点名必须与 `/proxies` 返回值完全一致，包括空格、符号和大小写。

### 3.6 AI Studio 无 Cookie 网页地区探测

延迟接口只能确认 Mihomo 完成了 HTTP 请求，不能读取最终跳转地址或页面正文。本项目因此会在延迟初筛通过后，通过运行配置的 `mixed-port` 再并发访问 `test_urls` 中的全部网址，并跟随网页重定向。

判定为可用：

- 最终域名是 `aistudio.google.com`；或
- 最终域名是 `accounts.google.com`，说明出口至少能到达 Google 登录入口。

判定为不可用：

- 跳转到 `ai.google.dev/.../available-regions`；
- 页面包含 `User location is not supported`；
- 页面包含 AI Studio 地区不可用提示；
- HTTP 状态为 4xx/5xx；
- 跳转到非预期域名；
- 连接、TLS 或读取超时。

此探针不读取 Google Cookie、Auth Token 或浏览器配置。它针对“账号本身有效，主要故障来自出口 IP”的场景，但不能发现只在登录后出现的账号级权限问题。

### 3.7 查询代理提供者

```http
GET /providers/proxies
GET /providers/proxies/{提供者名称}
```

它们用于查询 provider 元数据及节点列表。本项目当前主要使用 `/proxies` 返回的 `provider-name` 展示订阅来源，因为 Clash Verge Rev 将订阅内联到运行配置时，节点可能没有 `provider-name`，GUI 会将其显示为“本地配置”。

## 4. `config.json` 参数

示例：

```json
{
  "test_urls": [
    "https://aistudio.google.com",
    "https://hub.docker.com/"
  ],
  "interval_seconds": 60,
  "timeout_ms": 10000,
  "failure_threshold": 2,
  "advanced_web_probe": true,
  "allowed_redirect_hosts": ["aistudio.google.com", "accounts.google.com"],
  "blocked_url_keywords": ["available-regions"],
  "group": "🚀节点选择",
  "candidates": ["节点甲", "节点乙"],
  "controller_url": "",
  "controller_socket": ""
}
```

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `test_urls` | 字符串数组 | AI Studio、Docker Hub | 实际连通性测试网址；全部网址通过才算节点可用 |
| `interval_seconds` | 整数 | `60` | 一轮结束后到下一轮开始前的等待秒数，最小 5 秒 |
| `timeout_ms` | 整数 | `10000` | 每个节点单次探测超时毫秒数 |
| `failure_threshold` | 整数 | `2` | 当前节点连续失败多少轮后开始候选扫描 |
| `advanced_web_probe` | 布尔值 | `true` | 是否启用最终 URL、域名和地区页面特征判定 |
| `allowed_redirect_hosts` | 字符串数组 | AI Studio、Google Accounts | 网页探测允许的最终落点域名；默认值仅是预置配置，可完全修改 |
| `blocked_url_keywords` | 字符串数组 | `available-regions` | 最终 URL 命中任一关键词即判定失败 |
| `group` | 字符串 | 空 | 要切换的 `Selector` 策略组名称 |
| `candidates` | 字符串数组 | 空 | 只允许在这些节点中自动选择 |
| `controller_url` | 字符串 | 空 | 外部控制地址，例如 `http://127.0.0.1:9097` |
| `controller_socket` | 字符串 | 空 | Linux 自定义 Unix Socket 路径 |

`interval_seconds=60` 与 `failure_threshold=2` 表示当前节点通常需要连续失败约两分钟才触发切换。候选扫描耗时不计入这两分钟；如果多个节点各自等待 10 秒超时，完整扫描可能明显变长。

GUI 的“高级网页重定向判定”关闭后，程序只执行 Mihomo 延迟测试，不再临时切换候选做网页复检。启用时至少要允许一个最终域名。域名应只填写主机名，例如 `accounts.google.com`，不要填写协议或路径。保存配置时会自动把每个测试网址自身的主机名加入允许列表；如果服务还会跳转到其他域名，需要把相应最终域名加入列表。

多个测试网址使用“全部通过”语义。延迟初筛会并发请求这些网址，列表显示其中最大延迟；高级网页复检也会在当前候选节点选定后并发检查全部网址。不同候选节点不能并发网页复检，因为它们共享同一个 Selector。

## 5. 自动切换算法

每轮执行顺序：

1. 从目标 Selector 的 `now` 读取当前选择。
2. 使用当前选择执行 Mihomo 延迟初筛，再通过 `mixed-port` 执行无 Cookie 网页地区探测。
3. 成功则清零连续失败计数，并等待 `interval_seconds`。
4. 失败则累计计数；未达到 `failure_threshold` 时不切换。
5. 达到阈值后，逐个对 `candidates` 中除当前节点外的节点执行延迟初筛。
6. 临时切换到每个初筛成功的候选，再执行网页地区探测。
7. 收集所有两阶段检测均成功节点的 `(delay, name)`。
8. 选择 `delay` 最小的节点，并通过 `PUT /proxies/{group}` 切换。
9. 没有候选可用时恢复扫描开始前的节点。

候选网页复检必须先让实际业务策略组切到该候选，因此扫描期间该策略组承载的其他新连接也会短暂经过正在测试的节点。扫描不是并发执行，不会同时启动大量连接。

“最优节点”在本项目中的精确定义是：本轮延迟初筛和 AI Studio 网页地区探测均成功，且 Mihomo 延迟最低的已勾选候选。它不代表吞吐量最高、丢包最低或对所有网站都最优。

### GUI 立即测试

“立即测试”与后台自动切换的候选范围不同：它会测试当前 Selector 策略组中的全部真实节点，包括未勾选节点，用于帮助用户决定候选表。

- 测试开始后按钮显示最终判定总体进度。
- 所有节点的延迟初筛并发执行；任一节点初筛完成后立即在订阅来源左侧显示绿色 `延迟 ms` 或红色“失败”，不按节点顺序等待。
- 高级网页复检受共享 Selector 限制而逐节点执行；初筛绿色节点若命中失败规则，会立即更新为红色“失败”。
- 只有最后一个节点完成最终判定后才显示总结弹窗。
- 高级网页判定开启时，节点必须同时通过延迟和重定向规则。
- 整组测试会依次临时切换策略组，最终恢复测试开始前的节点。
- 后台监控服务运行时禁止整组测试，避免两个任务同时切换策略组。
- 订阅中的流量信息、到期信息等伪装节点通常会探测失败并显示为红色。

## 6. TUN、系统代理与节点探测

- 节点探测和切换本身不依赖 TUN，也不依赖系统代理。
- TUN 用于透明接管 Linux 服务器上其他进程的网络流量。
- 系统代理用于让遵循系统代理设置的应用连接 Mihomo 的 `mixed-port`。
- 即使 TUN 和系统代理都关闭，Mihomo 仍能执行 `/delay` 测试和策略组切换。
- 切换策略组只影响引用该策略组的流量链路；不引用该组的规则或入站不会随之改变。

## 7. Linux 后台运行

前台运行：

```bash
cd /opt/ClashVergeAutoSwitch
.venv/bin/python monitor_service.py
```

建议使用 `systemd` 管理。示例 `/etc/systemd/system/clash-verge-node-guard.service`：

```ini
[Unit]
Description=Clash Verge 节点守护
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/ClashVergeAutoSwitch
ExecStart=/opt/ClashVergeAutoSwitch/.venv/bin/python /opt/ClashVergeAutoSwitch/monitor_service.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clash-verge-node-guard.service
systemctl status clash-verge-node-guard.service
journalctl -u clash-verge-node-guard.service -f
```

如果 Clash Verge Rev 由桌面用户会话启动，必须保证守护服务用户有权限访问 Mihomo Unix Socket。可用以下命令确认实际路径和权限：

```bash
ls -l /tmp/verge/verge-mihomo.sock
```

## 8. 常见错误

| 现象 | 原因与处理 |
| --- | --- |
| `无法连接 Mihomo Unix Socket` | Clash 未运行、Socket 路径变化或服务用户无权限 |
| HTTP `401` | `secret` 不一致，检查 Clash 运行配置 |
| HTTP `404` | API 路径或 URL 编码错误 |
| HTTP `400` | 策略组不包含目标节点，或请求 JSON 不正确 |
| 节点全部超时 | 目标网址被节点出口阻断、DNS/证书问题或超时太短 |
| 切换成功但业务流量不变 | 业务规则未引用该 Selector，或业务流量未进入 Mihomo |
| 订阅更新后候选缺失 | 节点被改名或删除，需要在 GUI 重新勾选 |

## 9. 资料来源

- [Clash Verge Rev 官方仓库](https://github.com/clash-verge-rev/clash-verge-rev)
- [Clash Verge Rev 2.5.1 运行日志实例](https://github.com/clash-verge-rev/clash-verge-rev/issues/7393)
- [Clash Verge Rev 内部 IPC 与外部控制器说明](https://github.com/clash-verge-rev/clash-verge-rev/discussions/6951)
- [Linux Unix Socket 日志与说明](https://github.com/clash-verge-rev/clash-verge-rev/discussions/5272)
- [Mihomo 官方 REST API](https://wiki.metacubex.one/en/api/)
- [Mihomo 通用配置：external-controller](https://wiki.metacubex.one/en/config/general/)
