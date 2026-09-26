# 通用入口与隔离主 Agent 的关系

## 为什么不绑定某个聊天界面

用户要求没有限定入口。采用“独立运行核心 + 薄适配层”能同时保证可移植性和一致的隔离边界。外部客户端是任务提交者；主 Agent 是核心创建的隔离进程，不是用户正在使用的宿主聊天 Agent。

无论通过 CLI、MCP 还是 Pi 调用，主 Agent 都使用相同的提示词、委派工具和容器约束。无需依赖客户端是否撤销其内置工具。

## 统一任务协议

```json
{
  "task": "分析数据，生成文件，再委派独立 reviewer 验证",
  "inputs": {"samples.csv": "sample,conductivity\nA,10\nB,20\nC,30\n"},
  "timeout": 300
}
```

`task` 是自包含任务，`inputs` 是显式文本副本，`timeout` 是总运行秒数。没有 history、system_prompt、host_path、environment、model 或 backend 等字段。模型名称和输出位置只能由宿主启动配置设置，不允许模型工具参数改变。

客户端可能主动把自己的历史写成 task 文本，本项目不能识别所有语义泄漏；它保证不隐式继承宿主历史。

## CLI / Python 包

源码目录：

```powershell
python -m capsule run --request examples/request.json --model YOUR_MODEL
python -m capsule demo --test-process
```

安装到 Python 环境：

```powershell
python -m pip install .
capsule-agents --help
capsule-agents doctor
capsule-agents mcp --model YOUR_MODEL
```

安装包包含完整 Python 执行核心；Docker 镜像仍需使用仓库 Dockerfile 构建。安装包中的演示数据为内置测试常量，不依赖源码目录。

## MCP

将 examples/mcp-config.json 的服务器项加入客户端配置；具体配置位置由客户端决定。

支持 stdio JSON-RPC，协商版本 2024-11-05、2025-03-26、2025-06-18、2025-11-25。只公布 tools 能力，不声称支持 resources、prompts、HTTP 或客户端 sampling。模型服务由宿主配置。

`notifications/cancelled` 会设置取消事件，运行时中止等待并清理容器；输入流关闭也会取消当前任务。Python 无法强杀已经开始的网络请求线程，但该线程是 daemon，且底层 socket 有超时。

MCP 服务最多接受一个并发工具调用；运行期间仍响应 ping/cancel，额外调用返回忙错误。

## Pi

`pi install .` 后有三个命令：`/capsule-doctor`、`/capsule-demo`、`/capsule-run`。最后一个支持自然语言任务或上述 JSON 对象。Pi 扩展通过 stdin 传递 UTF-8 JSON，不拼接 shell 命令，不提取 Pi 历史。

Pi 命令适配器没有单独的取消命令，执行上限由任务 timeout 控制；需要协议级取消可使用 MCP。

## 与作业要求逐项对应

| 作业文字 | 实现 |
|---|---|
| 打包 | npm Pi 包、Python wheel、干净源码 ZIP |
| 主 Agent | 独立 main 容器；只有 delegate 工具 |
| 子 Agent | 每次委派新容器、新上下文、角色工具白名单 |
| 完全隔离 | 声明范围内的上下文/状态/权限/文件系统/网络隔离；通过任务和成果接口受控通信 |
| 插件 | Pi 适配器与标准 MCP 服务；核心不依赖特定 UI |

“完全”不是物理安全保证：容器共享内核，Broker 和模型服务可信，显式任务和成果属于允许的信息通道。

规范依据：[MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)。
