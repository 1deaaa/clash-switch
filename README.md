# Clash Verge 节点守护

跨 Windows 与 Linux 的 Mihomo/Clash Verge Rev 节点连通性监控器。默认每 60 秒并行测试全部已勾选候选以及当前节点，每个网址只请求一次，单次超时 5 秒。当前节点可用时保持不变；一旦失败，立即使用本轮已完成的成功结果或 90 秒内的上一轮缓存，切换到延迟最低的候选，不等待本轮慢节点或下一轮。测试期间不会为了检查候选而临时切换策略组。

## Windows

1. 保持 Clash Verge Rev 运行。
2. 双击 `clash-auto-switch.pyw`。如果系统没有关联 `.pyw`，则双击 `run_gui.bat`。
3. 选择策略组和候选节点，保存后点击“启动服务”。

测试网址支持多个值，以英文逗号分隔。所有网址都通过才认为节点可用，显示延迟取其中最大值。默认配置包含 `https://aistudio.google.com`、`https://hub.docker.com/` 和 `https://github.com/`。

“严格状态判定”默认开启。普通网址要求原始响应为 `2xx/3xx`；AI Studio 根地址会转换为 `/welcome` 并要求 `2xx`。Mihomo 的命名节点探测不跟随重定向，因此地区不受支持的跳转会直接记为失败，同时仍可对全部节点并行检测。

点击“查看节点”会打开候选节点面板，可按订阅来源筛选、勾选候选节点，并在面板内执行“立即测试”。整组测试会并行检查当前 Selector 策略组中的全部真实节点，不受候选勾选状态影响；节点可用显示为绿色延迟，不可用显示为红色“失败”，全程不切换当前策略组。后台服务运行期间不能执行整组测试。

当前节点右侧的“测试当前节点”只探测当前节点并立即显示结果；“立即切换”优先使用 90 秒内的新鲜成功结果，按已知最大延迟选择最优候选。没有新鲜结果时才并行探测候选，并在首个可用节点结果完成后立即切换，不等待所有慢节点。

主窗体日志默认只显示当前节点的探测结果，候选节点的整组结果保留在“查看节点”面板中。

程序优先读取 Clash Verge Rev 配置中的 `external-controller-pipe` 并连接本地命名管道，无需开启外部控制端口；管道不可用时才尝试 `controller_url`。若双击无反应，在 PowerShell 中执行：

```powershell
cd D:\Desktop\ClashVergeAutoSwitch
D:\APP\conda\python.exe app.py
```

## Kubuntu Linux

安装 Tk 和 Python 依赖：

```bash
sudo apt install python3-tk python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
chmod +x run_gui.sh
./run_gui.sh
```

Clash Verge Rev 通常通过 `/tmp/verge/verge-mihomo.sock` 提供控制接口，程序会自动发现。若 Linux 服务器没有桌面环境，可先在 Windows GUI 生成 `config.json`，迁移整个目录后运行：

```bash
.venv/bin/python monitor_service.py
```

在 Linux 桌面环境中，完成配置后点击 GUI 的“启动服务”会自动创建并启用用户级 systemd 服务。此后服务会在该用户登录时自动启动，GUI 通过 systemd 查询实际运行状态；点击“停止服务”只停止当前进程，下次登录仍会自动启动。

若 Unix Socket 路径不同，在 `config.json` 的 `controller_socket` 填入实际路径。也可将 `controller_url` 设为仅监听本机的外部控制地址，例如 `http://127.0.0.1:9097`。

## 切换规则

- 每轮先通过当前节点并发请求配置的全部测试网址，而不是只做 TCP ping。
- AI Studio 复检接受工作台或 Google 登录入口，拒绝地区支持说明页和地区错误正文。
- 默认连续失败 1 次即切换；可在界面中调高阈值，减少偶发网络抖动造成的频繁切换。
- 候选节点来自当前运行配置的 Selector 策略组，并显示其 `provider-name` 订阅来源。
- 没有任何候选节点通过测试时保持原节点。
- 候选探测通过 Mihomo 的指定节点接口完成，不会为了测试候选而切换当前策略组。
- `config.json` 不保存 Clash 控制密钥；程序从本机 Clash Verge Rev 配置读取。
