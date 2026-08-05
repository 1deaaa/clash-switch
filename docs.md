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

### 3.6 AI Studio 并行地区探测

Mihomo 的 `/proxies/{节点}/delay` 使用 `HEAD` 请求，并且不会跟随重定向。本项目利用这个行为把 `https://aistudio.google.com` 规范为 `https://aistudio.google.com/welcome`，同时传入 `expected=200-299`。支持地区应直接返回 `2xx`；地区不受支持、登录跳转或其他重定向会返回非 `2xx`，由 Mihomo 记为探测失败。

普通测试网址在严格判定开启时使用 `expected=200-399`。关闭严格判定后不传 `expected`，只要求 Mihomo 能通过该节点完成请求。

这种检测不读取 Cookie、Auth Token、页面正文或浏览器配置，不能发现只在登录后出现的账号级权限问题。它的优势是每个请求都明确绑定节点，可以并行检查全部候选，且不会改变业务 Selector。

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
    "https://hub.docker.com/",
    "https://github.com/"
  ],
  "interval_seconds": 60,
  "timeout_ms": 5000,
  "failure_threshold": 1,
  "advanced_web_probe": true,
  "allowed_redirect_hosts": ["aistudio.google.com", "accounts.google.com", "github.com"],
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
| `interval_seconds` | 整数 | `60` | 相邻两轮全量检测的目标起始间隔，最小 5 秒 |
| `timeout_ms` | 整数 | `5000` | 每个节点对每个网址的单次探测超时毫秒数；同一轮不重试 |
| `failure_threshold` | 整数 | `1` | 当前节点连续失败多少轮后执行切换；默认首次失败即切换 |
| `advanced_web_probe` | 布尔值 | `true` | 是否启用 HTTP 状态严格判定与 AI Studio `/welcome` 地区判定 |
| `allowed_redirect_hosts` | 字符串数组 | AI Studio、Google Accounts | 旧版兼容字段，当前并行探测不使用 |
| `blocked_url_keywords` | 字符串数组 | `available-regions` | 旧版兼容字段，当前并行探测不使用 |
| `group` | 字符串 | 空 | 要切换的 `Selector` 策略组名称 |
| `candidates` | 字符串数组 | 空 | 只允许在这些节点中自动选择 |
| `controller_url` | 字符串 | 空 | 外部控制地址，例如 `http://127.0.0.1:9097` |
| `controller_socket` | 字符串 | 空 | Linux 自定义 Unix Socket 路径 |

`interval_seconds=60` 与 `failure_threshold=1` 表示每轮开始时并行检测全部节点，当前节点首次失败就触发缓存切换。检测耗时包含在 60 秒周期内；若一轮耗时超过 60 秒，则跳过已经错过的时间点，避免连续启动多轮。

多个测试网址使用“全部通过”语义，显示延迟取其中最大值。全部“节点 × 网址”任务会各提交一次到同一线程池，单轮不重试；一个节点要等自身最慢的网址成功、失败或超时后才形成最终节点结果。单轮耗时主要由最慢请求决定，而不是节点数量乘以超时。

## 5. 自动切换算法

每轮执行顺序：

1. 从目标 Selector 的 `now` 读取当前选择。
2. 合并全部已勾选候选与当前节点，去重后并行执行本轮全部网址探测；每项只提交一次。
3. 每个节点的全部网址结束后立即更新其成功状态、最大延迟、检测时间和轮次缓存。
4. 当前节点成功则清零连续失败计数，保持 Selector 不变。
5. 当前节点失败并达到阈值后，优先从本轮已经完成的成功缓存中选择最低延迟节点；本轮尚无成功结果时，使用 90 秒内的上一轮成功缓存。
6. 立即通过一次 `PUT /proxies/{group}` 完成切换，不等待本轮其余慢节点；没有新鲜成功缓存时，等待本轮后续节点完成，一旦出现成功结果即切换。
7. 同一次当前节点故障只触发一次切换，本轮剩余探测继续完成并刷新缓存。
8. 扣除本轮耗时后等待，尽量让相邻轮次起点相隔 `interval_seconds`。

“最优节点”在本项目中的精确定义是：本轮配置的全部网址均探测成功，且最大延迟最低的已勾选候选。它不代表吞吐量最高、丢包最低或对所有网站都最优。

### GUI 立即测试

点击“查看节点”打开候选节点面板后，可以执行“立即测试”。它与后台自动切换的候选范围不同：会测试当前 Selector 策略组中的全部真实节点，包括未勾选节点，用于帮助用户决定候选表。

- 测试开始后按钮显示最终判定总体进度。
- 所有节点和测试网址并发执行；任一节点的全部网址完成后，立即在候选面板中显示绿色 `延迟 ms` 或红色“失败”，节点名称同步使用对应颜色。
- 只有最后一个节点完成最终判定后才显示总结弹窗。
- 严格状态判定开启时，节点必须同时满足目标网址的期望状态码。
- 整组测试不会切换策略组。
- 后台监控服务运行时禁止整组测试，避免两个任务同时切换策略组。
- 订阅中的流量信息、到期信息等伪装节点通常会探测失败并显示为红色。

当前节点区域提供两个手动操作：

- “测试当前节点”只探测当前节点，并把结果写入当前节点日志。
- “立即切换”先使用 90 秒内的新鲜成功缓存，按已知最大延迟选出最优候选；没有新鲜缓存时并行探测候选，首个可用结果完成后立即切换，不等待全部候选完成。

主窗体日志默认只保留当前节点的探测结果，不再展示整组候选的所有日志。

## 6. TUN、系统代理与节点探测

- 节点探测和切换本身不依赖 TUN，也不依赖系统代理。
- TUN 用于透明接管 Linux 服务器上其他进程的网络流量。
- 系统代理用于让遵循系统代理设置的应用连接 Mihomo 的 `mixed-port`。
- 即使 TUN 和系统代理都关闭，Mihomo 仍能执行 `/delay` 测试和策略组切换。
- 切换策略组只影响引用该策略组的流量链路；不引用该组的规则或入站不会随之改变。

## 7. Linux 后台运行

### 7.1 桌面 GUI 自动启动

完成配置后点击 GUI 的“启动服务”，程序会创建 `$XDG_CONFIG_HOME/systemd/user/clash-verge-node-guard.service`（未设置 `XDG_CONFIG_HOME` 时为 `~/.config/systemd/user/`），并执行等价的用户级启用与启动操作。服务使用启动 GUI 的 Python 解释器和项目绝对路径，因此会继承项目虚拟环境。

该服务会在用户登录后自动启动，能够访问 Clash Verge Rev 同一桌面会话创建的 Unix Socket。GUI 的服务状态取自 `systemctl --user is-active`，不依赖 PID 文件是否残留。GUI 中的“停止服务”只停止当前服务，自动启动仍保持启用状态。

用户级服务不能在没有登录该用户时可靠地访问桌面会话的 Mihomo Socket；不建议为这个场景启用 linger。需要在无桌面环境下运行时，使用下面的系统级服务方式，并改为稳定的外部控制器或确保 Socket 权限正确。

### 7.2 前台与系统级服务

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
