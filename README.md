# Clash Verge 节点守护

跨 Windows 与 Linux 的 Mihomo/Clash Verge Rev 节点连通性监控器。默认每 60 秒测试 AI Studio 与 Docker Hub；连续失败达到阈值后，仅测试你在 GUI 中勾选的候选节点，并切换到可用且延迟最低的节点。检测包含 Mihomo 延迟初筛，以及检查最终网页落点和地区拦截特征的无 Cookie 复检。

## Windows

1. 保持 Clash Verge Rev 运行。
2. 双击 `clash-auto-switch.pyw`。如果系统没有关联 `.pyw`，则双击 `run_gui.bat`。
3. 选择策略组和候选节点，保存后点击“启动服务”。

测试网址支持多个值，以英文逗号分隔。所有网址都通过才认为节点可用，显示延迟取其中最大值。默认配置包含 `https://aistudio.google.com` 和 `https://hub.docker.com/`。

“高级网页重定向判定”默认开启。可填写允许的最终域名及需要拦截的 URL 关键词；关闭后退回仅延迟探测。保存时会自动允许每个测试网址自身的域名。

“立即测试”会并发初筛当前 Selector 策略组中的全部真实节点，不受候选勾选状态影响。任一节点完成初筛后会立即在订阅来源左侧显示绿色延迟或红色“失败”，不等待其他节点。高级判定开启时，初筛成功节点随后依次做网页复检，命中失败规则会由绿色改为红色；全部最终判定完成后才弹总结。测试结束后恢复测试前节点；后台服务运行期间不能执行整组测试。

程序默认连接 `\\.\pipe\verge-mihomo`，无需开启外部控制端口。若双击无反应，在 PowerShell 中执行：

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

若 Unix Socket 路径不同，在 `config.json` 的 `controller_socket` 填入实际路径。也可将 `controller_url` 设为仅监听本机的外部控制地址，例如 `http://127.0.0.1:9097`。

## 切换规则

- 每轮先通过当前节点并发请求配置的全部测试网址，而不是只做 TCP ping。
- AI Studio 复检接受工作台或 Google 登录入口，拒绝地区支持说明页和地区错误正文。
- 默认连续失败 2 次才切换，减少偶发网络抖动造成的频繁切换。
- 候选节点来自当前运行配置的 Selector 策略组，并显示其 `provider-name` 订阅来源。
- 没有任何候选节点通过测试时保持原节点。
- 候选复检期间策略组会短暂切到正在测试的节点；全部失败时恢复扫描前节点。
- `config.json` 不保存 Clash 控制密钥；程序从本机 Clash Verge Rev 配置读取。
