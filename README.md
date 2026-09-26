# Pi Capsule Agents：基于显式授权与成果交付的隔离式主子 Agent 插件

这份作业从零实现独立运行时，并提供 **CLI、标准 MCP 和 Pi 插件**三个入口。核心问题是：**主、子 Agent 不共享历史、权限和工作区之后，如何仍然完成可检查的协作？**

答案是：主 Agent 只委派；子 Agent 只获得点名的数据副本；隔离容器内没有模型凭据和网络；成果由宿主校验、计算哈希并独立保存。主 Agent 可把成果引用交给新的 reviewer 做独立复核。

## 统一入口：一次实现，多种宿主

**入口与主 Agent 分开：聊天界面或命令行负责接收请求，真正的主 Agent 始终在独立容器里运行。** 换客户端不会改变隔离规则，也不需要继承某个聊天应用的历史。

| 入口 | 使用方式 | 适合场景 |
|---|---|---|
| CLI | `python -m capsule run --request examples/request.json --model MODEL` | 命令行、脚本、课堂演示 |
| Python 安装包 | `pip install .` 后执行 `capsule-agents` | 安装到任意目录后调用 |
| MCP stdio | `capsule-agents mcp` | 支持 MCP 的聊天/开发客户端 |
| Pi 插件 | `pi install .`，然后 `/capsule-run` | Pi 原生交互 |

三种入口共用 `capsule/api.py` 的 `run_request`，协议只接受 `task`、`inputs` 和 `timeout`。`history`、`workspace`、`environment`、`backend` 等额外参数都会被拒绝。MCP 提供 `capsule_run`、`capsule_demo`、`capsule_doctor` 三个工具；支持握手、工具发现、请求取消，一次只运行一个工具调用。它不是开放 HTTP 服务。

从源码运行无需 pip 安装：`python -m capsule --help`。MCP 配置见 [examples/mcp-config.json](examples/mcp-config.json)；需将模型名称替换为本机已安装的名称，并确保客户端能找到 `capsule-agents`。详细入口协议见 [ENTRYPOINTS.md](docs/ENTRYPOINTS.md)。

## 交付范围与边界

- 主 Agent 和每个子 Agent 各自使用新容器、新消息列表、独立进程和临时目录。
- 主 Agent 唯一工具为 `delegate`。Pi 中的命令只是启动入口，不是执行任务的主 Agent。
- 子 Agent 不继承 Pi 对话、系统提示词、Skills、AGENTS.md、环境变量或认证文件。
- 输入只通过 JSON 传递明确选中的 UTF-8 文本副本；没有宿主目录挂载。
- 容器 `network=none`，非 root、只读根目录、撤销 capabilities、限制内存/PID/CPU。
- 模型推理由宿主 Broker 请求本机 Ollama；容器既没有 API Key，也不能自己选择推理地址。
- 子 Agent 无委派工具；reviewer 没有写工具。工具在代码中按角色拒绝，不仅靠提示词。
- 子 Agent 只能提交文本成果，不会覆盖原始文件。Broker 校验名称、数量和大小后交付。
- 任何 Agent 都不能改变宿主设定的角色、镜像、执行预算和文件目录。

**“完全隔离”在这里指上述资源及上下文边界，不指绝对物理隔离。** Docker 共享宿主内核；宿主 Broker、Docker 和模型服务属于可信计算基础；模型仍可能答错或受显式输入影响。主 Agent 也可能主动把自己的信息写进委派任务，系统不声称识别所有语义泄漏。详细说明见 [设计与对比](docs/DESIGN_CN.md)。

## 环境

- Python 3.10+（宿主程序无第三方 Python 依赖）。
- Docker Engine / Docker Desktop，使用 Linux containers。
- 真实模型模式：已运行的本机 Ollama，并已下载一个能稳定输出 JSON 的模型。
- Pi 插件入口：Pi 0.87.1+；本次按本地示例 API 编写，其他版本需验证兼容性。
- 扩展自动测试直接加载 TypeScript，需要 Node.js 22.19+；本机使用 Node.js 24.19。

不自动安装软件、不自动下载模型、不自动读取用户工作区。Docker 不可用时，正式运行直接失败，不会降级成不安全的子进程。

## 1. 构建与检查

在本目录执行：

```powershell
docker build -t pi-capsule-agents:1.0.0 .
python capsule/cli.py doctor
```

第一次构建需要下载基础镜像。正式部署建议将 Dockerfile 的基础镜像改为已核验的 digest；当前固定版本 tag 仍可能被上游更新。

## 2. 无模型、无费用的演示

```powershell
python capsule/cli.py demo
```

它启动三个容器：main → analyst → reviewer。测试数据 conductivity 为 10、20、30。analyst 计算均值并提交 `analysis.json`；reviewer 在全新上下文中重算并核对。main 返回复核结果。

**demo 的模型是确定性测试替身，证明流程可执行，不证明真实大模型的能力。** 容器隔离则真实执行，前提是使用上述默认 Docker 后端。

没有 Docker 时，仅用于开发验证：

```powershell
python capsule/cli.py demo --test-process
```

此命令有真实的独立 Python 进程，但**没有操作系统沙箱**；输出标记 `backend=test-process`。真实 `run` 模式拒绝这个选项。

## 3. 真实主、子 Agent

先通过 `ollama list` 找到已经安装的模型，将下方 `YOUR_INSTALLED_MODEL` 替换为实际名称。

```powershell
python capsule/cli.py run --model YOUR_INSTALLED_MODEL --task "分析 conductivity 数据，生成结论文件，再委派独立 reviewer 复算；汇总证据和局限" --input samples.csv=examples/samples.csv
```

也支持无文件任务及文本资料：

```powershell
python capsule/cli.py run --model YOUR_INSTALLED_MODEL --task "委派 author 起草一份实验日志模板，再由 reviewer 检查遗漏"
```

可重复传入 `--input NAME=FILE`。左侧为 Agent 看到的别名；右侧路径仅宿主读取，不交给 Agent。每份文件不超过 32KiB，总输入不超过 64KiB；本版本只支持 UTF-8 文本，不支持 PDF/图片/二进制。

## 4. 安装成 Pi 插件

```powershell
pi install .
$env:CAPSULE_MODEL="YOUR_INSTALLED_MODEL"
# 如果 python 不在 PATH，设置完整可执行文件路径：
# $env:CAPSULE_PYTHON="D:\anaconda\python.exe"
pi
```

在 Pi 内执行：

```text
/capsule-doctor
/capsule-demo
/capsule-run 委派 author 写一份实验记录规范，并委派 reviewer 审核
```

命令只传递你在命令后输入的任务，不传递 Pi 当前历史。也可以直接传入 JSON：`/capsule-run {"task":"分析数据并独立复核","inputs":{"samples.csv":"sample,conductivity\nA,10\nB,20\nC,30\n"}}`。读取本地文件请使用 CLI 的显式 `--input`；MCP 与 Pi 不接受任意宿主路径。每次调用都是独立任务，不支持续接历史。

## 输出与验收

```text
.capsule-output/<run-id>/
├── audit.jsonl                 # 生命周期、授权引用、内容哈希，不保存完整模型会话
├── result.json                 # 成功/失败、模型类型、摘要和产物列表
└── artifacts/<agent-id>/
    └── analysis.json           # UTF-8 原始成果；不会自动运行
```

`completed` 表示流程成功结束，不保证事实正确；reviewer 的 PASS 在真实模型模式下仍是模型判断。SHA-256 证明交付字节是否变化，不证明内容正确，也不是防管理员篡改的数字签名。

## 测试

```powershell
python -m unittest discover -s tests -v
# 已构建 Docker 镜像后，再启用真实容器集成测试：
$env:CAPSULE_DOCKER_TESTS="1"
python -m unittest discover -s tests -v
Remove-Item Env:CAPSULE_DOCKER_TESTS
```

覆盖未授权文件拒绝、角色权限、路径穿越、代码表达式拒绝、结果校验、主子上下文分离、独立复核、真实子进程往返和异常状态。Docker 测试检查完整演示及非 root、环境变量不继承、临时目录不复用、网络不可达、无 Docker socket。

Pi 入口测试：`node --test tests/extension.test.mjs`。生成干净的源码提交包：`python scripts/package.py`，文件输出到项目父目录的 `deliverables/`。包内不包括运行日志、缓存或同学仓库。

当前交付机器没有 Docker，实际运行记录见 [验证记录](docs/VALIDATION.md)。未执行的测试不会记作通过。

## 代码导航

| 文件 | 内容 |
|---|---|
| `capsule/worker.py` | 主/子 Agent JSON 动作循环、角色工具、受限计算、消息交互 |
| `capsule/runtime.py` | Docker 启动、宿主模型代理、授权校验、交付、预算和审计 |
| `capsule/cli.py` | 命令行入口、显式输入读取、模式限制 |
| `capsule/api.py` | 所有入口共用的任务协议与边界校验 |
| `capsule/mcp.py` | 标准 stdio MCP 服务、工具发现与取消 |
| `extensions/index.ts` | Pi 命令注册及异步执行入口 |
| `tests/test_capsule.py` | 单元、真实进程、可选 Docker 集成测试 |
| `docs/DESIGN_CN.md` | 原创设计说明、同学作业对照、威胁模型和限制 |
| `docs/DEMO_CN.md` | 课堂演示及答辩问题 |

依赖文档：[Pi 扩展](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)、[Docker 容器运行](https://docs.docker.com/engine/containers/run/)、[Ollama Chat API](https://docs.ollama.com/api/chat)。
