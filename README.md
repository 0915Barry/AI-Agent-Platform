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

`start` 会同步代码、检查 rootfs 并启动仅监听 Ubuntu loopback 的 Control Plane；
`status` 和 `stop` 直接查询或停止已部署服务，不再重复同步。停止控制面不会删除
用户持久化数据盘。实例计算资源由空闲回收策略管理，默认空闲阈值为 300 秒。
每个账户使用 1 条后台实例记录和 1 块持久化数据盘；工作区是该数据盘上的逻辑分组，
新会话使用 Pi 原生 JSONL Session 并保存在数据盘的 `pi-state` 中，不会为每个工作区
或会话重复创建 microVM 和数据盘。

macOS 如需从本机访问 HTTP 控制面，另开一个终端保持 SSH 隧道运行：

```bash
./run.sh tunnel
```

该命令正常情况下不会继续输出；它正在保持
`127.0.0.1:18090 → Ubuntu 127.0.0.1:18090` 的端口转发。按 `Ctrl-C` 关闭隧道。

## 终端 CLI

`agentctl` 通过 Control Plane 管理工作区、会话和诊断实例，不直接连接 Firecracker
或 Pi RPC。`./agentctl workspace` 使用方向键创建或切换工作区；`./agentctl chat`
从当前工作区选择已有会话或新建会话，后台会在首次发送消息时自动准备 microVM。
macOS 使用前应先运行 `./run.sh start`，并在另一个终端保持 `./run.sh tunnel`；Windows
则直接在已经启动 Control Plane 的 VMware Ubuntu 终端中使用。

首次登录：

```bash
./agentctl login <username>
```

密码不会回显。会话 Cookie 以 `0600` 权限保存在用户配置目录，不写入仓库。
日常使用只需选择工作区和会话：

```bash
./agentctl workspace
./agentctl chat
```

`workspace` 会显示工作区、会话数量和 `MM/DD HH:MM` 格式的最近活动时间；
`chat` 显示当前工作区中的会话、最近时间和最多 10 个字的消息预览。两个菜单
都支持上下方向键、Enter 和 Esc。首条消息会自动启动后台 microVM；输入
`/exit` 只退出对话，不停止实例。选中已有工作区或会话后，子菜单可选择
“打开 / 删除 / 取消”。删除原生会话会先停止当前实例，再清理数据盘中的 Pi Session、
数据库元数据、任务和事件；删除工作区会二次校验名称，并同时清理该工作区的会话和
用户盘目录。

实例命令仅保留给开发者诊断：

```bash
./agentctl status
./agentctl stop <instance-id>
```

用户持久盘不随运行实例停止或销毁而删除。不支持 ANSI 颜色的终端可用：

```bash
./agentctl --no-color status
```

CLI 默认连接 `http://127.0.0.1:18090`，也可使用 `--base-url` 或 `AAP_BASE_URL` 指定
其他 Control Plane 地址。

自动化调试也可显式指定工作区或会话：

```bash
./agentctl workspace --new "AI Agent Platform"
./agentctl workspace --select <workspace-id>
./agentctl chat --workspace <workspace-id> --conversation <conversation-id>
```
继续旧会话时，CLI 会先展示最近 10 条历史消息，再进入 SSE 流式对话。

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
| 终端完整链路 | `./run.sh cli-smoke-test` | `./run-linux.sh cli-smoke-test` |
| 异常恢复 | `./run.sh recovery-smoke-test` | `./run-linux.sh recovery-smoke-test` |

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

CLI 完整链路成功标志：

```text
PASS: agentctl completed authenticated lifecycle, chat, restart, and cleanup
CLI_SMOKE_READY auth=session lifecycle=create,start,stop,restart,destroy conversation=preserved cleanup=verified
```

恢复链路成功标志：

```text
PASS: Control Plane restart and Firecracker crash recovery preserved instance data
RECOVERY_READY control_plane=reconnected firecracker_crash=reaped state=reconciled network=clean data=preserved restart=verified
```

## Control Plane 与 Pi Gateway

Control Plane 提供实例、任务、事件、会话和认证 API。它只监听 Ubuntu 的
`127.0.0.1:18090`，不应直接暴露到局域网或公网。

工作区与会话主要接口：

```http
GET    /api/workspaces
POST   /api/workspaces
POST   /api/workspaces/{workspace_id}/select
DELETE /api/workspaces/{workspace_id}
GET    /api/workspaces/{workspace_id}/conversations
POST   /api/workspaces/{workspace_id}/conversations
DELETE /api/workspaces/{workspace_id}/conversations/{conversation_id}
```

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

用户、工作区、会话索引、任务和事件与实例、运行状态、网络租约及用户数据盘归属
统一保存在 Ubuntu 宿主的 `/var/lib/fc/control-plane.db` 中。新会话以平台
`conversation_id` 作为 Pi `--session-id`，完整原生 Session 由 Pi 保存到用户数据盘；
数据库只保存标题、预览、消息数量和 Session 定位信息，完整历史通过 Pi RPC 从原生
Session 读取。模型上下文也由 Pi Session 自行恢复，不再由控制面重复拼接。升级前已有
会话暂时继续使用数据库上下文模式，避免原历史丢失。
升级前的 `/var/lib/fc/tasks/tasks.db` 会在服务首次启动时自动导入，原文件暂时保留作为备份。

## 网络与数据边界

Network Manager 从 `172.29.0.0/16` 地址池按 `/30` 分配槽位。每个运行实例获得一个
宿主机网关地址、一个 Guest 地址、独立 TAP 和 nftables 表。租约状态保存在实例管理
目录的 SQLite 中；停止或启动失败时按顺序清理规则、路由和 TAP，再释放租约。

主要数据位置：

| 数据 | Ubuntu Host 位置 |
|---|---|
| 内核与 rootfs | `/srv/fc/artifacts/` |
| 用户唯一持久化数据盘 | `/srv/fc/volumes/users/<user-id>.ext4` |
| Jailer 临时目录 | `/srv/jailer/firecracker/<instance-id>/` |
| 控制面数据库 | `/var/lib/fc/control-plane.db` |
| 空闲活动与长任务标记 | `/var/lib/fc/activity/`、`/var/lib/fc/instances/*.busy` |
| DeepSeek API Key | `~/.config/ai-agent-platform/deepseek-api-key` |

Git 仓库不保存 API Key、SSH 私钥、实例磁盘、Jailer 目录或运行日志。模型密钥只由
Ubuntu Host 的 Tool Gateway 读取，不写入 rootfs 或实例数据盘。

## 构建与运行速度

第一次构建会从固定 Ubuntu 快照生成 rootfs，并安装 Node.js 和 Pi Agent，因此耗时
较长。后续会根据构建指纹复用 `/srv/fc/artifacts/agent-rootfs.ext4`；只有版本、构建
脚本或注入文件发生变化时才更新镜像。出现大量 `I: Retrieving ...` 表示正在执行完整
Ubuntu rootfs 构建，不是每次任务都会发生。

如果 Ubuntu Snapshot 服务返回 503，脚本会自动重试；服务持续不可用时，稍后重新运行
同一命令即可。

### microVM 冷启动优化

在 Apple Silicon + UTM + Ubuntu ARM64 开发环境中，受管 microVM 冷启动最初实测约为
`11.75 s`。该耗时并非主要来自数据盘、TAP 或 nftables，而是 Guest 每次启动都
重复执行 Pi CLI 版本检查、静态权限验证，Host 还以较大的时间间隔轮询 Guest 就绪标记。

本轮优化包括：

- 在 rootfs 构建时真实执行 Node.js 和 Pi 版本验证，并将结果写入只读系统盘的
  `/etc/ai-agent-platform-build`。Guest 启动时只读取该构建元数据，不再重复启动
  Node.js 和 Pi CLI。这是最主要的优化，消除了原来约 `6.45 s` 的重复版本检查。
  `runtime-smoke-test` 会显式启用验收模式，在真实 Guest 中再运行一次 Node.js、Pi 和
  UID 检查，因此完整验证能力没有被取消。验收时约 `6.5 s` 的版本检查耗时
  是预期行为，不属于正式实例的冷启动时间。
- 将 Node 文件所有者、`/etc/shadow` 访问权限和 `/usr/local` 写权限等静态安全
  检查移到 rootfs 构建阶段。Guest 每次启动仍以 `pi` 用户实际写入一次
  `/workspace`，验证本次挂载的用户数据盘权限，因此没有牺牲运行时安全验证。
- 将 Host 等待 `PI_RPC_READY` 的轮询间隔从 `250 ms` 缩短为 `100 ms`，减少 Guest
  已就绪但 Host 尚未发现的额外等待。
- 在 Instance Manager 和 Guest 启动脚本中增加分阶段计时，并由 Control Plane
  通过 SSE 传递给 `agentctl`。CLI 会在冷启动后以亮粉色逐行显示 Host 和 Guest
  各阶段耗时，便于后续定位回归。

优化后同一环境的实测冷启动降至约 `5.00 s`，相比最初减少约 `57%`。
最近一次样本中，Host 的数据盘、网络和模型网关准备总计不到 `0.4 s`；主要剩余瓶颈是
Guest 内核启动并进入 init，约为 `3.07 s`。因此继续压缩几十毫秒的 Host 操作收益
有限；如需进一步显著缩短启动时间，后续应优先评估预热实例或 Firecracker 快照恢复。
上述数据是开发环境样本，实际耗时会随 CPU、磁盘和外层虚拟化环境变化。

需要重新验证 Guest 内的真实版本时，运行：

```bash
./run.sh runtime-smoke-test
```

成功输出中的 `verification=executed` 表示 Node.js、Pi 和 UID 命令确实已在 Guest 内执行，
而不是仅读取构建元数据。

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
