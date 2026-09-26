# 验证记录

日期：2026-09-26。环境：Windows，Python 3.12.7，Node.js v24.19.0。

## 1.1.0 入口完善验证

- Python 共 25 项测试：23 项通过，2 项 Docker 测试在本机跳过。
- Node 扩展测试 1 项通过。
- 新增统一协议拒绝多余字段/后端覆盖、任务取消清理、MCP 初始化/工具发现/取消、真实 stdio UTF-8 往返测试。
- 已构建 Python wheel，支持 CLI、Python module、MCP；Pi 适配器支持 JSON 请求。
- 添加 GitHub Actions，在 Linux/Windows 执行单元测试，并在 Linux 构建 Docker 镜像及执行容器测试。上传前尚无 CI 运行结果，不预先声称通过。

以下为 1.0.0 初始记录，保留用于区分测试范围。

## 已实际执行

| 检查 | 结果 |
|---|---|
| `python -m unittest discover -s tests -v` | 16 项中 14 项通过、2 项 Docker 测试跳过 |
| `node --test tests/extension.test.mjs` | 1 项通过；扩展导入、命令注册、参数拒绝 |
| `python capsule/cli.py demo --test-process --output output` | 成功；3 个独立 Agent 进程，2 次委派，10 次测试模型调用 |
| 演示产物 | `count=3, min=10, max=30, mean=20`；独立 reviewer 复算一致 |
| 产物 SHA-256 | `b464104c5d79359d996ae797b62582c74e2f51659076be01660276faeb7eaba1` |
| `python capsule/cli.py doctor` | 预期失败：未发现 Docker CLI |

测试报告中 `test-process` 的上下文与协议隔离已验证，但它不提供系统沙箱。不能把这些结果改写为“Docker 隔离已通过”。

## 未执行

- Docker 镜像构建、Docker 容器端到端演示及内核边界检查：未安装 Docker。
- Ollama 真实模型端到端调用：未发现已配置的 Ollama 可执行程序，没有自动安装或下载模型。
- Pi 应用内安装与交互演示：未发现本机 Pi 可执行程序。已验证 TypeScript 扩展可加载及注册 API 调用；未执行完整 SDK 类型检查。

## 下一环境的验收命令

```powershell
docker build -t pi-capsule-agents:1.0.0 .
python capsule/cli.py doctor
$env:CAPSULE_DOCKER_TESTS="1"
python -m unittest discover -s tests -v
python capsule/cli.py demo
```

上述应让全部 16 项 Python 测试通过。再使用 README 的真实模型命令完成模型联调，并在 Pi 中执行 `/capsule-demo`。

## 结果的含义

测试说明程序在已覆盖条件下拒绝越权并完成显式数据协作。它不证明所有模型任务正确、不证明绝无信息泄漏、不证明不同主机的 Docker 配置相同。

截至本记录，包是完整源码和已验证的进程级原型；容器部署和真实模型联调仍需要上述环境验收。没有虚构费用、速度或成功率优势。
