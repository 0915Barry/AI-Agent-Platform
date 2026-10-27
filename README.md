# AI Agent Platform

在 Firecracker microVM 中隔离运行 Pi Agent 的技术原型。当前仓库聚焦底层运行平台：
实例生命周期、网络租约、持久化数据盘、宿主机 Pi Gateway、模型凭据隔离、任务事件、
会话和本地账户。当前分支不包含前端页面。

macOS 开发者在本机运行 `run.sh`，脚本通过 SSH 操作 UTM 中的 Ubuntu；Windows
开发者在 VMware 中的 Ubuntu VM 内运行 `run-linux.sh`。Firecracker 始终运行在具有
`/dev/kvm` 的 Ubuntu 环境中，不直接运行在 macOS 或 Windows 上。

## 当前架构

```text
macOS + UTM / Windows + VMware
└── Ubuntu KVM Host
    ├── Control Plane：实例、任务、会话、账户与状态
    ├── Instance Manager：准备 Jailer、磁盘、网络并启动 Firecracker
    ├── Network Manager：分配 /30 网络租约、TAP、路由和 nftables
    ├── Pi Gateway：连接 Guest Pi RPC，发送任务并接收 JSONL 事件
    ├── Tool Gateway：代持 DeepSeek API Key 并转发模型请求
    └── Firecracker microVM
        ├── 只读 rootfs.ext4
        ├── 可写 data.ext4
        └── socat TCP:8080 ↔ pi --mode rpc stdin/stdout
```

正式任务路径：

```text
终端或其他客户端 → Control Plane → SQLite queued 任务
→ Host Pi Gateway → Guest IP:8080 → socat → Pi RPC
→ Pi JSONL 事件 → Pi Gateway → SQLite / SSE
```

旧 Agent Bridge、动态 MCP、动态 Skill、Workspace HTTP 通道和 React/Vite 页面不属于
当前技术原型。完整旧网页版本可从 Git 标签 `mvp-web-m19` 查看。

## 支持范围

已实机验证：

- Apple Silicon Mac + UTM + Apple Virtualization；
- Ubuntu Server 24.04 ARM64；
- Ubuntu 内可读写 `/dev/kvm`；
- Firecracker、Jailer、Pi RPC、DeepSeek 和网络租约端到端链路。

已提供但仍需 Windows 实机验收：

- Windows 10/11 x86_64 + VMware Workstation；
- Ubuntu Server 24.04 AMD64；
- VMware 向 Ubuntu 暴露 VT-x/EPT 或 AMD-V/RVI。

Windows 不应在 PowerShell、Git Bash 或 WSL2 中直接执行 `run-linux.sh`。仓库、镜像和
数据盘必须放在 VMware Ubuntu 的原生 ext4 磁盘中。

## macOS + UTM 首次部署

### 1. 创建 Ubuntu VM

在 UTM 中创建 Ubuntu Server 24.04 ARM64 VM，并满足：

- 使用 Apple Virtualization；
- CPU 架构为 ARM64；
- 磁盘建议至少 64 GiB；
- 安装 OpenSSH Server；
- Ubuntu 用户具有 `sudo` 权限。

安装完成后，在 Ubuntu 终端检查：

```bash
uname -m
cat /etc/os-release
ls -l /dev/kvm
groups
```

预期 `uname -m` 为 `aarch64`，且当前用户能够访问 `/dev/kvm`。如果用户不在 `kvm`
组，可执行下列命令，然后完整注销并重新登录：

```bash
sudo usermod -aG kvm "$USER"
```

使用 `hostname -I` 获取 Ubuntu VM 地址，并从 macOS 终端先验证：

```bash
ssh agentdev@<Ubuntu-VM-IP>
```

### 2. 克隆并配置

在 macOS 终端执行：

```bash
git clone https://github.com/0915Barry/AI-Agent-Platform.git
cd AI-Agent-Platform
./run.sh bootstrap <Ubuntu-VM-IP> agentdev
./run.sh configure-user
./run.sh configure-deepseek
./run.sh verify
```

`bootstrap` 会保存连接设置、检查宿主机和 Ubuntu、同步仓库、安装固定版本
Firecracker，并构建或复用运行镜像。SSH 密码和远程 `sudo` 密码可能会被分别询问。

## Windows + VMware 首次部署

### 1. 创建 Ubuntu VM

在 VMware Workstation 中创建 Ubuntu Server 24.04 AMD64 VM：

- 启用嵌套虚拟化；
- 磁盘建议至少 64 GiB；
- 安装 OpenSSH Server；
- 仓库位于 Ubuntu 原生磁盘，而不是 VMware 共享目录。

在 Ubuntu VM 终端检查：

```bash
uname -m
cat /etc/os-release
ls -l /dev/kvm
groups
sudo dmesg | grep -Ei 'kvm|vmx|svm|virtualization' | tail -n 50
```

预期 `uname -m` 为 `x86_64`。如果 `/dev/kvm` 不存在，应先检查 BIOS、Hyper-V/VBS
占用和 VMware 的嵌套虚拟化设置，不要继续运行项目脚本。

### 2. 在 Ubuntu 中克隆并配置

以下命令全部在 VMware Ubuntu 终端执行：

```bash
git clone https://github.com/0915Barry/AI-Agent-Platform.git
cd AI-Agent-Platform
chmod +x run-linux.sh scripts/linux/*.sh scripts/guest/*.sh
./run-linux.sh bootstrap
./run-linux.sh configure-user
./run-linux.sh configure-deepseek
./run-linux.sh verify
```

脚本会根据 `uname -m` 自动选择 AMD64 依赖，不会下载 ARM64 构建物。

## 日常启动与停止

macOS 在仓库根目录执行：

```bash
./run.sh start
./run.sh status
./run.sh stop
```

Windows 在 VMware Ubuntu 的仓库目录执行：

```bash
./run-linux.sh start
./run-linux.sh status
./run-linux.sh stop
```

`start` 启动仅监听 Ubuntu loopback 的 Control Plane；`stop` 停止控制面，但不会删除
实例持久化数据盘。实例计算资源由空闲回收策略管理，默认空闲阈值为 300 秒。

macOS 如需从本机访问 HTTP 控制面，另开一个终端保持 SSH 隧道运行：

```bash
./run.sh tunnel
```

该命令正常情况下不会继续输出；它正在保持
`127.0.0.1:18090 → Ubuntu 127.0.0.1:18090` 的端口转发。按 `Ctrl-C` 关闭隧道。

## 终端 CLI

`agentctl` 通过 Control Plane 管理实例和会话，不直接连接 Firecracker 或 Pi RPC。
macOS 使用前应先运行 `./run.sh start`，并在另一个终端保持 `./run.sh tunnel`；Windows
则直接在已经启动 Control Plane 的 VMware Ubuntu 终端中使用。

首次登录：

```bash
./agentctl login <username>
```

密码不会回显。会话 Cookie 以 `0600` 权限保存在用户配置目录，不写入仓库。随后可以
完成实例的完整操作：

```bash
./agentctl create demo-agent
./agentctl start demo-agent
./agentctl status demo-agent
./agentctl chat demo-agent
./agentctl stop demo-agent
./agentctl destroy demo-agent
```

`chat` 使用 SSE 显示流式回答，并以不同颜色区分用户输入、Pi 输出、工具调用、系统
状态和错误。输入 `/exit` 只退出对话，不停止实例。`destroy` 会永久删除实例数据，默认
要求再次输入实例 ID；自动化环境必须显式添加 `--yes`。不支持 ANSI 颜色的终端可用：

```bash
./agentctl --no-color status
```

CLI 默认连接 `http://127.0.0.1:18090`，也可使用 `--base-url` 或 `AAP_BASE_URL` 指定
其他 Control Plane 地址。

## 核心验收

`verify` 会执行账户认证、网络租约以及真实 Pi Gateway 任务链路。需要拆分排错时，按
下列顺序运行：

| 能力 | macOS | VMware Ubuntu |
|---|---|---|
| 环境与 KVM | `./run.sh vm-doctor` | `./run-linux.sh doctor` |
| 最小 microVM | `./run.sh microvm-smoke-test` | `./run-linux.sh microvm-smoke-test` |
| Pi 运行镜像 | `./run.sh runtime-smoke-test` | `./run-linux.sh runtime-smoke-test` |
| Jailer 权限 | `./run.sh security-smoke-test` | `./run-linux.sh security-smoke-test` |
| 出站网络策略 | `./run.sh network-smoke-test` | `./run-linux.sh network-smoke-test` |
| 网络租约 | `./run.sh network-lease-smoke-test` | `./run-linux.sh network-lease-smoke-test` |
| 持久化数据盘 | `./run.sh persistence-smoke-test` | `./run-linux.sh persistence-smoke-test` |
| Tool Gateway | `./run.sh gateway-smoke-test` | `./run-linux.sh gateway-smoke-test` |
| 空闲回收 | `./run.sh lifecycle-smoke-test` | `./run-linux.sh lifecycle-smoke-test` |
| 认证与归属 | `./run.sh auth-smoke-test` | `./run-linux.sh auth-smoke-test` |
| HTTP 实例控制 | `./run.sh control-plane-smoke-test` | `./run-linux.sh control-plane-smoke-test` |
| Host Pi Gateway | `./run.sh agent-task-smoke-test` | `./run-linux.sh agent-task-smoke-test` |

网络租约成功标志：

```text
PASS: managed instance Pi RPC, network allocation, and cleanup completed without a model request
MANAGED_RPC_READY guest=172.29.0.2:8080 transport=tcp protocol=jsonl bridge=absent mcp=deferred
NETWORK_LEASE_READY pool=172.29.0.0/16 subnet=172.29.0.0/30 ... state=released
```

Pi Gateway 成功标志：

```text
PASS: host Pi Gateway dispatched a real task to microVM Pi RPC
PI_GATEWAY_READY guest=172.29.0.2:8080 transport=tcp protocol=jsonl dispatch=host events=streamed bridge=absent
```

## Control Plane 与 Pi Gateway

Control Plane 提供实例、任务、事件、会话和认证 API。它只监听 Ubuntu 的
`127.0.0.1:18090`，不应直接暴露到局域网或公网。

主要实例接口：

```http
POST   /api/instances
GET    /api/instances
GET    /api/instances/{id}
POST   /api/instances/{id}/start
POST   /api/instances/{id}/heartbeat
POST   /api/instances/{id}/stop
DELETE /api/instances/{id}
```

任务与事件接口：

```http
POST /api/instances/{id}/tasks
GET  /api/instances/{id}/tasks/{task_id}
GET  /api/instances/{id}/tasks/{task_id}/events
GET  /api/instances/{id}/tasks/{task_id}/stream
```

Pi Gateway 从实例运行元数据读取 `guestIp` 和 `rpcPort`，通过 TCP 连接 Guest 的
Pi RPC。它将 Pi 原生 JSONL 事件标准化为 `progress`、`text`、`tool_call`、
`tool_result`、`completed` 或 `failed`，写入 SQLite，并通过 SSE 流输出。一个实例同一
时间串行处理任务，任务执行期间设置生命周期 `busy` 标记。

会话和消息保存在 Ubuntu 宿主 SQLite 中。每次发送消息时，控制面按顺序选取同一会话
最近的消息组装上下文；显式销毁实例时才删除关联任务和会话记录。

## 网络与数据边界

Network Manager 从 `172.29.0.0/16` 地址池按 `/30` 分配槽位。每个运行实例获得一个
宿主机网关地址、一个 Guest 地址、独立 TAP 和 nftables 表。租约状态保存在实例管理
目录的 SQLite 中；停止或启动失败时按顺序清理规则、路由和 TAP，再释放租约。

主要数据位置：

| 数据 | Ubuntu Host 位置 |
|---|---|
| 内核与 rootfs | `/srv/fc/artifacts/` |
| 实例数据盘 | `/srv/fc/volumes/instances/` |
| Jailer 临时目录 | `/srv/jailer/firecracker/<instance-id>/` |
| 实例运行元数据 | `/var/lib/fc/instances/` |
| DeepSeek API Key | `~/.config/ai-agent-platform/deepseek-api-key` |

Git 仓库不保存 API Key、SSH 私钥、实例磁盘、Jailer 目录或运行日志。模型密钥只由
Ubuntu Host 的 Tool Gateway 读取，不写入 rootfs 或实例数据盘。

## microVM 学习模式

`lab-*` 命令用于观察最基础的 Jailer、磁盘、TAP 与 Pi RPC 结构，不依赖页面和业务
数据库：

```bash
./run.sh lab-start
./run.sh lab-status
./run.sh lab-inspect
./run.sh lab-stop
```

启动后 SSH 进入 Ubuntu，并按输出执行：

```bash
nc <Guest-IP> 8080
```

然后可发送一行 Pi RPC JSONL：

```json
{"id":"demo-1","type":"get_state"}
```

`lab-stop` 删除临时 Jailer、TAP 和 nftables 规则，但保留
`/srv/fc/volumes/lab/data.ext4`。

## 构建与运行速度

第一次构建会从固定 Ubuntu 快照生成 rootfs，并安装 Node.js 和 Pi Agent，因此耗时
较长。后续会根据构建指纹复用 `/srv/fc/artifacts/agent-rootfs.ext4`；只有版本、构建
脚本或注入文件发生变化时才更新镜像。出现大量 `I: Retrieving ...` 表示正在执行完整
Ubuntu rootfs 构建，不是每次任务都会发生。

如果 Ubuntu Snapshot 服务返回 503，脚本会自动重试；服务持续不可用时，稍后重新运行
同一命令即可。

## 可配置参数

macOS 的连接参数保存在仓库根目录 `.env`，该文件已被 Git 忽略：

```dotenv
VM_HOST=<Ubuntu-VM-IP>
VM_USER=agentdev
REMOTE_DIR=/opt/ai-agent-platform
UTM_APP_PATH=/Applications/UTM.app
```

共享脚本不得写入开发者姓名、个人目录或固定 VM IP。Firecracker、内核、Node.js 和
Pi Agent 的版本与校验值统一维护在 `config/versions.env`。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `/dev/kvm` 不存在 | 外层 VM 未暴露嵌套虚拟化；返回 UTM/VMware 与 BIOS 设置检查 |
| `/dev/kvm` 权限失败 | 将用户加入 `kvm` 组并完整重新登录 |
| hard link 或 cross-device 错误 | 镜像或数据盘不在同一 Ubuntu 原生文件系统 |
| SSH 连接失败 | 在 Ubuntu 重查 `hostname -I`，先单独验证 SSH，再重新 `configure` |
| DeepSeek 401/403 | 重新运行 `configure-deepseek`，不要把 Key 写入 `.env` |
| `Unknown command` | 本地代码版本过旧；更新仓库后重新执行 |

## 项目文档

规划与架构文档位于 [`docs/`](./docs/README.md)。README 只维护当前代码可执行的环境
搭建、运行和验收方法；尚未实现的快照、预热池、生产集群等设计不作为当前命令承诺。
