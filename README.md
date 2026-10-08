# AI Agent Platform MVP

在隔离的 Firecracker microVM 中运行 Pi Agent 的企业智能体平台 MVP。

当前仓库按“先脚本化、再执行、最后验收”的方式推进。macOS 开发者在宿主机运行 `run.sh`，脚本通过 SSH 操作 UTM Linux；Windows 开发者在 VMware Ubuntu VM 内运行 `run-linux.sh`，避免依赖不一致的 Windows Shell 工具。

## 项目文档

规划和架构设计存放在 [`docs/`](./docs/README.md)，所有环境搭建与运行说明只维护在本 README：

- [企划书](./docs/企划书.md)
- [架构与实现方案汇报](./docs/架构与实现方案汇报.md)
- [服务器单机 MVP 架构设计图](./docs/服务器单机MVP架构设计图.md)

## 快速开始

首次部署**不需要**从 M0 到 M12 逐项运行。里程碑命令是底层研发、故障定位和
回归测试入口；首次运行只使用 `bootstrap → configure-deepseek → verify → start`。

开始前仍需人工创建一台支持嵌套虚拟化的 Ubuntu 24.04 VM：Mac 使用 UTM +
Apple Virtualization + ARM64 Ubuntu，Windows x86_64 使用 VMware Workstation +
AMD64 Ubuntu。VM 必须存在可读写的 `/dev/kvm`，并将仓库/镜像放在 Ubuntu 原生
ext4 磁盘中。这部分涉及宿主 BIOS、公司安全策略和虚拟机安装，仓库不能自动代办。

### macOS 首次部署

在 Mac 克隆仓库后执行：

```bash
git clone https://github.com/0915Barry/AI-Agent-Platform.git
cd AI-Agent-Platform

# 首次安装：检查 Mac/UTM/KVM，同步代码，安装 Firecracker 并准备 rootfs
./run.sh bootstrap <UTM-Linux-IP> agentdev

# 隐藏输入并把 Key 保存到 Ubuntu VM，不写入仓库或 microVM
./run.sh configure-deepseek

# 首次部署建议执行一次完整端到端验收
./run.sh verify

# 日常开发只启动长期运行的控制面
./run.sh start
./run.sh status
```

需要使用 M12 页面时，再开两个 macOS 终端分别运行：

```bash
# 终端 2：把只监听 Ubuntu loopback 的控制面安全转发到 Mac
./run.sh tunnel

# 终端 3：安装依赖（首次）并启动本地页面
./run.sh web-dev
```

浏览器打开 `http://127.0.0.1:5173`。结束开发时分别按 `Ctrl-C` 停止页面和隧道，
再运行 `./run.sh stop`；停止控制面不会删除实例数据盘。

### Windows + VMware 首次部署

在 VMware 的 Ubuntu VM 内克隆仓库并执行：

```bash
git clone https://github.com/0915Barry/AI-Agent-Platform.git
cd AI-Agent-Platform
chmod +x run-linux.sh scripts/common/*.sh scripts/linux/*.sh scripts/guest/*.sh

./run-linux.sh bootstrap
./run-linux.sh configure-deepseek
./run-linux.sh verify
./run-linux.sh start
./run-linux.sh status
```

需要使用 M12 页面时，在 Ubuntu VM 再开一个终端运行：

```bash
./run-linux.sh web-dev
```

然后在 Windows PowerShell 建立仅本机可见的页面隧道：

```powershell
ssh -N -L 5173:127.0.0.1:5173 agentdev@<Ubuntu-VM-IP>
```

Windows 浏览器打开 `http://127.0.0.1:5173`。结束开发时按 `Ctrl-C` 停止页面隧道，
在 Ubuntu 运行 `./run-linux.sh stop`。不要在 PowerShell、Git Bash 或 WSL2 中直接
执行 `run-linux.sh`。

`bootstrap` 是一个幂等总入口，内部仍按检查、安装、下载和构建分阶段执行。当前
仓库尚未发布预构建的 ARM64/AMD64 rootfs，因此每台全新机器第一次仍需完成一次
本地构建，耗时不会因为合成一个命令而消失；之后构建指纹不变时会直接复用
`/srv/fc/artifacts/agent-rootfs.ext4`。未来发布经过校验的双架构 runtime bundle 后，
`bootstrap` 可以改为优先下载，失败时再回退到本地构建。

## 当前进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | macOS、Apple Silicon、UTM 环境检查 | ✅ 已通过 |
| M1 | UTM Linux 获得可读写的 `/dev/kvm` | ✅ 已通过 |
| M2 | Firecracker v1.17.0 启动 ARM64 最小 microVM | ✅ 已通过 |
| M3 | 可重建 Ubuntu rootfs、Node.js 与 Pi Agent | ✅ 已通过 |
| M4 | 实例内权限与 Firecracker jailer 加固 | ✅ 已通过 |
| M5 | microVM 网络与出站治理 | ✅ 已通过 |
| M6 | 只读 rootfs 与独立持久化数据盘 | ✅ 已通过 |
| M7 | Tool Gateway 与凭据外置 | ✅ 已通过 |
| M8 | 实例生命周期与空闲回收 | ✅ 已通过，正式阈值 5 分钟 |
| M9 | Pi Agent → Tool Gateway → DeepSeek 真实联调 | ✅ 已通过 |
| M10 | HTTP 控制面管理真实 microVM 生命周期 | ✅ 已通过 |
| M11 | 控制面向 Pi Agent 下发任务并经 DeepSeek 返回结果 | ✅ 已通过 |
| M12 | React 管理页面操作实例并提交 Agent 任务 | ✅ 已通过 |

## 当前支持范围

已经实机验收：

- macOS 15 或更高版本
- Apple M3/M4
- UTM 4.6 或更高版本
- UTM 使用 Apple Virtualization，而不是 QEMU Emulation
- ARM64 Ubuntu 24.04 Linux VM
- Linux VM 中当前用户可以读写 `/dev/kvm`

已实现但等待 Windows 实机验收：

- x86_64 Windows 10/11 宿主机
- VMware Workstation 中的 Ubuntu Server 24.04 AMD64
- VMware 向 Ubuntu 暴露 VT-x/EPT 或 AMD-V/RVI，Ubuntu 获得可读写的 `/dev/kvm`
- Ubuntu VM 内通过 `run-linux.sh` 执行全部构建和验收

Windows 不使用 UTM，也不直接运行 Firecracker。两个平台最终都把 Firecracker 放在具有 `/dev/kvm` 的 Ubuntu VM 内：

```text
macOS Apple Silicon                 Windows x86_64
└── UTM + Apple Virtualization      └── VMware Workstation
    └── Ubuntu ARM64                    └── Ubuntu AMD64
        └── Firecracker                     └── Firecracker
            └── Agent microVM                  └── Agent microVM
```

在第一台真实 Windows 设备完成全部测试前，Windows 路径仍标记为“待实机验收”。

## macOS + UTM 完整环境搭建

### 1. 准备宿主机

要求 Apple Silicon Mac、至少 16 GB 内存、macOS 15 或更高版本，以及 UTM 4.6 或更高版本。执行：

```bash
sw_vers
uname -m
system_profiler SPHardwareDataType | sed -n '1,20p'
/Applications/UTM.app/Contents/MacOS/utmctl version
```

`uname -m` 必须输出 `arm64`。下载 Ubuntu Server 24.04 ARM64 ISO，例如：

```text
ubuntu-24.04.x-live-server-arm64.iso
```

不要使用 AMD64/x86_64 ISO。

### 2. 创建 UTM Ubuntu VM

1. 在 UTM 中选择“新建虚拟机 → Virtualize → Linux”。
2. 使用 Apple Virtualization，并选择“Boot from ISO image”。不要使用 QEMU Emulation。
3. 建议分配 4 个 vCPU、8 GB 内存和至少 64 GB 虚拟磁盘。
4. 网络使用默认 Shared Network/NAT。
5. Shared Directory 留空；Firecracker 镜像、jail 和数据盘必须位于 Ubuntu 原生 ext4 文件系统。
6. 选择 ARM64 Ubuntu Server ISO 并启动安装。

### 3. 安装 Ubuntu Server

安装器建议选择：

- 安装类型：`Ubuntu Server`，不必选择 minimized。
- 第三方驱动：不勾选。
- Proxy address：没有明确代理时留空。
- Ubuntu mirror：使用通过测试的默认镜像。
- 存储：`Use an entire disk`；LVM 可以保留，开发 VM 不必启用 LUKS。
- Server name：例如 `agent-platform-dev`。
- 用户名：推荐 `agentdev`。
- SSH：勾选 `Install OpenSSH server`；密码认证仅用于本地开发网络。
- Featured Server Snaps：全部不选。

安装结束后重启，确保 UTM 已弹出安装 ISO，不再从 ISO 启动。

### 4. 检查 Ubuntu 与嵌套 KVM

在 Ubuntu 终端执行：

```bash
uname -m
cat /etc/os-release
ls -l /dev/kvm
groups
sudo dmesg | grep -Ei 'kvm|hyp|el2|virtualization'
test -r /dev/kvm && test -w /dev/kvm && echo 'KVM access OK'
```

应看到 `aarch64`、Ubuntu 24.04、存在 `/dev/kvm`，并最终输出 `KVM access OK`。如果设备存在但用户无权限：

```bash
sudo usermod -aG kvm "$USER"
```

随后完整注销 Ubuntu 会话并重新登录。

### 5. 获取 VM 地址并测试 SSH

在 Ubuntu 中运行：

```bash
hostname -I
```

记下类似 `192.168.64.3` 的地址。在 macOS 终端测试：

```bash
ssh agentdev@<UTM-Linux-IP>
```

确认可以登录后输入 `exit` 返回 macOS。VM 重启后 IP 可能变化；届时重新运行后面的 `configure` 即可。

### 6. 在 macOS 克隆并初始化环境

仓库保留在 macOS，`run.sh` 会通过 SSH/rsync 将源码同步到 Ubuntu 的 `/opt/ai-agent-platform`：

```bash
git clone https://github.com/0915Barry/AI-Agent-Platform.git
cd AI-Agent-Platform
./run.sh setup <UTM-Linux-IP> agentdev
```

`setup` 会保存被 Git 忽略的本机 `.env`，检查 macOS/UTM 和 Ubuntu/KVM，同步仓库，并安装固定版本的 Firecracker 与 jailer。以后 VM 地址变化时运行：

```bash
./run.sh configure <新IP> agentdev
```

### 7. 启动最小 microVM

仍在 macOS 仓库根目录运行：

```bash
./run.sh microvm-smoke-test
```

成功标志：

```text
PASS: Firecracker booted the aarch64 microVM and reached guest init
```

## Windows + VMware 完整环境搭建

### 1. 明确支持边界

当前试点仅支持 x86_64 Windows 10/11，不支持 Windows on ARM。Windows 只是最外层宿主；不要直接在 Windows、WSL2 或 Git Bash 中运行 Firecracker 和项目 Shell 脚本。

如果公司策略不允许 VMware 获得嵌套虚拟化能力，应改用裸机或远程 Linux KVM 服务器。不要擅自关闭公司设备的 Hyper-V、VBS 或“内存完整性”。参考：

- [Firecracker Getting Started](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
- [Microsoft Hyper-V Nested Virtualization](https://learn.microsoft.com/en-us/virtualization/hyper-v-on-windows/user-guide/nested-virtualization)
- [Broadcom VMware 嵌套虚拟化排错](https://knowledge.broadcom.com/external/article/389469/virtualized-intel-vtxept-is-not-supporte.html)

### 2. 检查 Windows 与硬件

要求 Intel VT-x/EPT 或 AMD-V/RVI 已在 BIOS/UEFI 中开启，建议宿主机至少 16 GB 内存。先在“任务管理器 → 性能 → CPU”确认“虚拟化：已启用”，也可以在命令提示符运行：

```powershell
systeminfo.exe
```

安装 VMware Workstation，并下载 Ubuntu Server 24.04 AMD64 ISO：

```text
ubuntu-24.04.x-live-server-amd64.iso
```

不要下载 ARM64 ISO。

### 3. 创建 VMware Ubuntu VM

1. 创建 Ubuntu 24.04 64-bit VM，网络建议使用 NAT。
2. 完全关闭 VM，打开 `VM Settings → Hardware → Processors`。
3. 勾选 `Virtualize Intel VT-x/EPT or AMD-V/RVI`。
4. 建议分配 4 个 vCPU、8 GB 内存和至少 80 GB 虚拟磁盘。
5. 仓库、Firecracker 镜像和数据盘必须放在 Ubuntu 原生 ext4 磁盘；不要放在 VMware Shared Folders、SMB 共享或 Windows 挂载目录。
6. 挂载 AMD64 Ubuntu Server ISO 并启动安装。

如果 VMware 报 `Virtualized Intel VT-x/EPT is not supported on this platform`，通常是 Hyper-V/VBS 正在占用虚拟化能力。公司设备先联系 IT，再按 Broadcom 官方文档处理；关闭这些功能可能影响 WSL2、Windows Sandbox 和安全保护。

### 4. 安装 Ubuntu Server

Ubuntu 安装选项与 macOS 路径保持一致：使用普通 `Ubuntu Server`、Proxy 留空、整盘安装、可保留 LVM、安装 OpenSSH Server、Featured Server Snaps 全部不选。推荐用户名仍为 `agentdev`。

### 5. 检查 Ubuntu 与嵌套 KVM

登录 Ubuntu VM，执行：

```bash
uname -m
cat /etc/os-release
grep -Eoc '(vmx|svm)' /proc/cpuinfo
ls -l /dev/kvm
groups
test -r /dev/kvm && test -w /dev/kvm && echo 'KVM access OK'
```

正确结果必须同时满足：

- `uname -m` 输出 `x86_64`。
- `vmx`/`svm` 计数大于 `0`。
- `/dev/kvm` 存在。
- 最后一条输出 `KVM access OK`。

如仅缺少权限，运行：

```bash
sudo usermod -aG kvm "$USER"
```

随后完整注销并重新登录。如果 `/dev/kvm` 不存在，应回到 VMware/BIOS 检查，不要继续执行项目脚本。

### 6. 在 Ubuntu 原生磁盘中克隆仓库

Windows 路径的 Git 和项目命令全部在 Ubuntu VM 终端执行：

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/0915Barry/AI-Agent-Platform.git
cd AI-Agent-Platform
chmod +x run-linux.sh scripts/linux/*.sh scripts/guest/*.sh
```

Git clone 通常会保留可执行位；`chmod` 用于兼容 ZIP 解压或错误的 Git 文件模式设置。不要在 Ubuntu VM 内运行面向 Mac 控制端的 `run.sh`。

### 7. 检查环境并启动最小 microVM

```bash
./run-linux.sh doctor
./run-linux.sh setup
./run-linux.sh microvm-smoke-test
```

只有 `doctor` 输出以下内容才继续：

```text
KVM access: OK
Linux/KVM checks passed
```

最小 microVM 成功标志中的架构应为 `x86_64`。脚本会自动选择 AMD64 Ubuntu、Node.js x64 和经过校验的 x86_64 Firecracker 构建物，不会下载 ARM64 文件。

## 开发者分层验收（仅用于回归与排错）

最小 microVM 成功后按顺序执行。上一项失败时不要继续下一项：

| 阶段 | macOS 仓库根目录 | Windows 的 Ubuntu VM 仓库目录 |
|---|---|---|
| 环境检查 | `./run.sh doctor`、`./run.sh vm-doctor` | `./run-linux.sh doctor` |
| 安装 Firecracker | 已包含在 `setup` | 已包含在 `setup` |
| 最小 microVM | `./run.sh microvm-smoke-test` | `./run-linux.sh microvm-smoke-test` |
| Pi 运行镜像 | `./run.sh runtime-smoke-test` | `./run-linux.sh runtime-smoke-test` |
| 权限与 jailer | `./run.sh security-smoke-test` | `./run-linux.sh security-smoke-test` |
| 网络策略 | `./run.sh network-smoke-test` | `./run-linux.sh network-smoke-test` |
| 持久化 | `./run.sh persistence-smoke-test` | `./run-linux.sh persistence-smoke-test` |
| Tool Gateway | `./run.sh gateway-smoke-test` | `./run-linux.sh gateway-smoke-test` |
| 生命周期 | `./run.sh lifecycle-smoke-test` | `./run-linux.sh lifecycle-smoke-test` |
| 保存 DeepSeek Key | `./run.sh configure-deepseek` | `./run-linux.sh configure-deepseek` |
| DeepSeek 端到端 | `./run.sh deepseek-e2e-test` | `./run-linux.sh deepseek-e2e-test` |
| HTTP 控制面 | `./run.sh control-plane-smoke-test` | `./run-linux.sh control-plane-smoke-test` |
| Agent 任务通道 | `./run.sh agent-task-smoke-test` | `./run-linux.sh agent-task-smoke-test` |

第一次 `runtime-smoke-test` 会从固定 Ubuntu 快照构建完整 rootfs，下载较多基础包并安装 Node.js 和 Pi Agent，耗时明显较长。构建指纹不变时后续测试会复用 `/srv/fc/artifacts/agent-rootfs.ext4`。

### 命令作用

| 命令 | 作用 |
|---|---|
| `bootstrap` | 新机器的一键入口：检查环境、安装 Firecracker、准备内核并构建或复用 rootfs |
| `verify` | 运行当前最高层 M11 端到端验收；新机器建议执行一次 |
| `start` / `stop` / `status` | 日常启动、停止和查询长期运行的控制面 |
| `tunnel`（仅 `run.sh`） | 将 Mac 的 `127.0.0.1:18090` 安全转发到 Ubuntu loopback 控制面 |
| `web-install` | 安装 lockfile 固定的前端依赖；Linux 自动使用项目固定的 Node.js |
| `web-build` | 执行 TypeScript 检查并生成生产构建 |
| `web-dev` | 在 `127.0.0.1:5173` 启动 M12 开发页面和控制面反向代理 |
| `doctor` | 检查当前控制端或 Linux/KVM 环境 |
| `configure`（仅 `run.sh`） | 保存 Mac 到 UTM Linux 的连接设置 |
| `sync`（仅 `run.sh`） | 通过 rsync 将源码同步到 UTM Linux |
| `install-firecracker` | 幂等安装固定版本 Firecracker 与 jailer |
| `prepare-microvm` | 下载并校验与 CPU 架构匹配的内核和 initramfs |
| `microvm-smoke-test` | 启动最小 microVM 并验证 guest init |
| `build-rootfs` | 构建固定 Ubuntu、Node.js 和 Pi Agent rootfs |
| `runtime-smoke-test` | 验证 rootfs、Node、Pi Agent 和非 root 用户 |
| `security-smoke-test` | 验证 guest 权限与 Firecracker jailer 加固 |
| `network-smoke-test` | 验证 TAP、HTTPS 出站和私有网络默认拒绝 |
| `persistence-smoke-test` | 验证只读系统盘与独立持久化数据盘 |
| `gateway-smoke-test` | 验证宿主侧凭据注入、脱敏审计和上游隔离 |
| `lifecycle-smoke-test` | 以 6 秒测试阈值验证心跳、空闲回收、重启和销毁 |
| `configure-deepseek` | 隐藏输入并将 API Key 保存到 Linux 用户私有目录 |
| `deepseek-e2e-test` | 让 Pi Agent 经 Tool Gateway 调用 DeepSeek 并执行 `read` 工具 |
| `control-plane-smoke-test` | 通过 HTTP API 创建、启动、查询、心跳、停止并销毁真实 microVM |
| `agent-task-smoke-test` | 通过控制面向 microVM 内 Pi 下发真实任务，并验证 DeepSeek、工具读取和事件结果 |
| `control-plane-start` | 构建所需镜像并在 Ubuntu loopback 启动长期运行的控制面 |
| `control-plane-stop` | 停止控制面进程；已启动实例仍由独立空闲回收器管理 |
| `control-plane-status` | 查询控制面服务是否运行 |

运行 `./run.sh help` 或 `./run-linux.sh help` 可以查看对应入口支持的完整命令。

## M3 Agent 运行镜像

最小 microVM 验收通过后运行：

```bash
# macOS 仓库根目录
./run.sh runtime-smoke-test

# Windows 的 Ubuntu VM 仓库目录
./run-linux.sh runtime-smoke-test
```

该命令会在 Ubuntu KVM 宿主中完成构建并启动验收。第一次需要下载 Ubuntu 包、Node.js 和 Pi Agent，耗时会明显长于最小启动测试；相同构建指纹再次执行时会复用已验证的镜像。Ubuntu 系统包来自配置文件中固定日期的官方快照，构建完成后还会保存实际安装的软件包清单。

Ubuntu 快照服务偶尔会出现单个软件包的瞬时下载失败。构建脚本会自动重试三次；如果整个命令仍然失败，可以直接重新执行，已校验的 Node.js 和 Pi Agent 下载缓存会被复用，未完成的 rootfs 不会替换已有镜像。

只有同时出现 `PASS: runtime rootfs booted...` 和 `AGENT_RUNTIME_READY...` 才表示本里程碑完成；软件包安装结束本身不代表 microVM 已通过启动验收。

成功标志类似：

```text
PASS: runtime rootfs booted with pinned Node, Pi Agent, and non-root pi user
AGENT_RUNTIME_READY node=v22.19.0 pi=1.0.0 uid=1001
```

基础运行镜像故意不包含模型密钥，也不预置网络配置。后续里程碑按测试场景挂载数据盘、注入受控网络，并通过宿主侧 Tool Gateway 使用模型凭据。

### M3 验收结果

已在 Apple M4、UTM Apple Virtualization、Ubuntu 24.04 ARM64 与嵌套 KVM 环境中实际通过：

```text
PASS: runtime rootfs booted with pinned Node, Pi Agent, and non-root pi user
AGENT_RUNTIME_READY node=v22.19.0 pi=1.0.0 uid=1001
```

这证明固定版本的 Ubuntu rootfs 可以重建，Firecracker 可以从该 rootfs 启动，且 Node.js 与 Pi Agent 能够由非 root 的 `pi` 用户执行。当前仍属于功能基线：rootfs 暂时可写，Firecracker 暂时由测试脚本直接启动，jailer、网络、数据盘和生命周期管理尚未启用。

## M4 权限与 jailer

在接入网络前先完成两层安全验收：

1. microVM 内部：系统运行时归 `root:root`，`pi` 只能写 `/workspace` 与 `/home/pi`，无法读取 `/etc/shadow` 或覆盖 Node/Pi。
2. Ubuntu KVM 宿主上：使用专用 `firecracker` 用户和 jailer 启动 VMM，验证 chroot、seccomp、零 capabilities、空环境变量、文件描述符上限和设备访问范围。

“隔离”指实例无法访问宿主和其他租户资源，不要求实例无法识别自己运行在虚拟化环境中。

运行对应平台命令：

```bash
# macOS
./run.sh security-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh security-smoke-test
```

成功时会同时输出 guest 权限与 jailer 进程验收标志：

```text
PASS: guest permissions and Firecracker jailer hardening checks passed
AGENT_SECURITY_READY runtime_owner=0:0 shadow=denied runtime_write=denied workspace_write=allowed
JAILER_SECURITY_READY uid=<专用用户UID> gid=<专用属组GID> seccomp=all:<线程数> capabilities=0 environment=0 nofile=2048 chroot=isolated devices=kvm,tun
```

### M4 验收结果

已在当前 ARM64 嵌套 KVM 开发环境中实际通过：

```text
PASS: guest permissions and Firecracker jailer hardening checks passed
AGENT_SECURITY_READY runtime_owner=0:0 shadow=denied runtime_write=denied workspace_write=allowed
JAILER_SECURITY_READY uid=999 gid=988 seccomp=all:4 capabilities=0 environment=0 nofile=2048 chroot=isolated devices=kvm,tun
```

专用用户的具体 UID/GID 由目标 Linux 系统分配，不属于跨机器固定接口；验收关注的是 Firecracker 不以 root 运行，并且其所有线程均受 seccomp、零 capabilities、空环境变量、chroot 和资源上限约束。

## M5 网络与出站治理

运行对应平台命令：

```bash
# macOS
./run.sh network-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh network-smoke-test
```

测试会自动创建临时 TAP 和独立 `/30` 网段，自动识别 UTM Linux 的默认出口网卡，并通过独立 nftables 表仅允许 DNS 与 HTTPS 出站。RFC1918、链路本地地址、其他未授权端口以及主动进入 guest 的连接默认拒绝。规则只匹配测试 TAP，不修改 SSH 所在的普通 INPUT 流量；成功、失败或中断时都会清理 TAP、临时 jail、防火墙表并恢复原 IP 转发状态。

成功标志：

```text
PASS: jailed microVM networking, HTTPS egress, and private-network denial checks passed
AGENT_NETWORK_READY ip=172.31.254.2/30 gateway=172.31.254.1 dns=resolved https=allowed private=denied
NETWORK_POLICY_READY tap=fc-tap-smoke subnet=172.31.254.0/30 egress=<自动识别> private_drop_packets=<大于0> cleanup=armed
```

### M5 验收结果

已在当前 UTM Linux 环境中实际通过：

```text
PASS: jailed microVM networking, HTTPS egress, and private-network denial checks passed
AGENT_NETWORK_READY ip=172.31.254.2/30 gateway=172.31.254.1 dns=resolved https=allowed private=denied
NETWORK_POLICY_READY tap=fc-tap-smoke subnet=172.31.254.0/30 egress=enp0s1 private_drop_packets=2 cleanup=armed
```

`enp0s1` 是本次运行自动识别出的 UTM Linux 出口网卡，并未写死在共享脚本中。`private_drop_packets=2` 证明私有地址拒绝规则实际命中过测试流量。测试退出后，临时 TAP、nftables 表、jailer 目录和 IP 转发状态均由退出清理逻辑处理。

## M6 只读系统盘与持久化数据盘

运行对应平台命令：

```bash
# macOS
./run.sh persistence-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh persistence-smoke-test
```

测试会创建一个临时工作区数据盘，使用只读 rootfs 启动第一台 jailed microVM 并由 `pi` 写入随机标记；随后停止 VMM、删除临时 jail、检查数据盘，再创建第二台 jailed microVM 挂载同一数据盘并读取标记。系统盘写入必须失败，工作区写入必须成功。

成功标志：

```text
AGENT_PERSISTENCE_WRITTEN token=<随机标记> rootfs=readonly
AGENT_PERSISTENCE_READY token=<相同标记> rootfs=readonly data=preserved
PASS: read-only rootfs and persistent data volume survived microVM recreation
PERSISTENCE_POLICY_READY rootfs=readonly data_volume=/srv/fc/volumes/smoke/persistence.ext4 token=<相同标记> jail=recreated
```

验收数据盘保留在 `/srv/fc/volumes/smoke/persistence.ext4`，再次运行测试时会重建该专用测试盘。生产工作区数据盘不会因实例停止或 jail 清理而删除。

### M6 验收结果

已使用同一数据盘跨两次 jailed microVM 重建实际通过：

```text
AGENT_PERSISTENCE_WRITTEN token=efc6d6dacea94db7 rootfs=readonly
AGENT_PERSISTENCE_READY token=efc6d6dacea94db7 rootfs=readonly data=preserved
PASS: read-only rootfs and persistent data volume survived microVM recreation
PERSISTENCE_POLICY_READY rootfs=readonly data_volume=/srv/fc/volumes/smoke/persistence.ext4 token=efc6d6dacea94db7 jail=recreated
```

这证明实例临时状态与用户数据已经分离：jail 和 VMM 可以销毁重建，系统盘保持只读，工作区数据仍由独立数据盘保留。

## M7 Tool Gateway 与凭据外置

运行对应平台命令：

```bash
# macOS
./run.sh gateway-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh gateway-smoke-test
```

该测试不会使用真实模型密钥，也不会产生模型费用。脚本会在 UTM Linux 上生成一次性随机测试凭据，并以 `0400` 权限仅交给专用 `agent-gateway` 用户；microVM 内只有无效占位凭据。Tool Gateway 只监听测试 TAP 地址，只允许 `/v1/chat/completions` 路由，并在转发给仅监听 loopback 的模拟上游时注入真实测试凭据。

验收同时检查：

1. microVM 使用占位凭据仍能通过 Gateway 请求模拟上游。
2. microVM 直接访问上游端口会被 nftables 拒绝，并产生实际命中计数。
3. Gateway 凭据来自宿主文件且文件权限为 `0400`。
4. guest 控制台、Firecracker 配置、Gateway 日志和审计日志均不包含实际凭据。
5. 审计仅记录租户、路径、状态和凭据来源等元数据，不记录 Authorization 内容。

成功标志：

```text
PASS: Tool Gateway injected a host credential without exposing it to the microVM
AGENT_GATEWAY_READY credential=placeholder upstream=authorized direct_upstream=denied response=gateway-smoke-response
TOOL_GATEWAY_READY bind=172.31.253.1:18080 credential=host-file:0400 upstream=loopback:18081 audit=redacted direct_upstream=denied drop_packets=<大于0>
```

测试结束后，一次性凭据、临时 TAP、nftables 表和 jail 会自动删除。脱敏审计日志保留在 UTM Linux 的 `/var/lib/fc/gateway/`，用于排查验收结果；它们不进入 Git。生产接入真实模型供应商时，应将同一接口后的测试文件凭据替换为密钥管理服务，并保持 microVM 不持有供应商密钥这一边界。

### M7 验收结果

已在当前 ARM64 嵌套 KVM 环境中实际通过：

```text
PASS: Tool Gateway injected a host credential without exposing it to the microVM
AGENT_GATEWAY_READY credential=placeholder upstream=authorized direct_upstream=denied response=gateway-smoke-response
TOOL_GATEWAY_READY bind=172.31.253.1:18080 credential=host-file:0400 upstream=loopback:18081 audit=redacted direct_upstream=denied drop_packets=2
```

这证明 microVM 不需要持有上游凭据：请求只能进入 TAP 地址上的 Gateway，由宿主侧注入权限为 `0400` 的凭据，再转发到仅监听 loopback 的上游。`drop_packets=2` 证明绕过 Gateway 的直连尝试实际命中了拒绝规则；控制台、服务日志、配置和审计记录均未泄露该凭据。

## M8 实例生命周期与空闲回收

运行对应平台命令：

```bash
# macOS
./run.sh lifecycle-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh lifecycle-smoke-test
```

本次测试按约定使用 6 秒空闲阈值。测试会先启动实例并写入随机数据，然后验证 `.busy` 长任务标记能够阻止回收、心跳能够刷新最后活动时间；停止发送心跳后，回收器必须自动关闭 Firecracker，并只清理该实例登记的 jail、TAP 和 nftables 表。随后使用同一数据盘重启实例，确认数据仍然存在，最后验证只有显式 `destroy` 才会删除测试数据盘。

生命周期状态存放在 UTM Linux 的 `/var/lib/fc/instances/` 与 `/var/lib/fc/activity/`。状态中同时保存 PID 启动标识，避免 PID 被操作系统复用后误杀无关进程。停止顺序为 guest 关机请求、SIGTERM、SIGKILL 逐级兜底；普通停止和空闲回收保留数据盘。

成功标志类似：

```text
INSTANCE_HEARTBEAT_EXTENDED id=lifecycle-smoke timeout=6 busy=protected heartbeat=extended
INSTANCE_REAPED id=lifecycle-smoke reason=idle_timeout ... volume=preserved
INSTANCE_IDLE_REAP_READY id=lifecycle-smoke timeout=6s ephemeral=cleaned volume=preserved
INSTANCE_RESTART_READY id=lifecycle-smoke data=preserved stop=clean
PASS: lifecycle manager extended active sessions, reaped idle compute, and preserved restart data
LIFECYCLE_POLICY_READY default_idle=6s smoke_idle=6s busy=protected heartbeat=extended ephemeral=cleaned restart_data=preserved explicit_destroy=verified
```

正式实例默认使用 `config/lifecycle.env` 中的 `INSTANCE_IDLE_TIMEOUT_SECONDS=300`，即连续 5 分钟没有活动才进入回收。`LIFECYCLE_SMOKE_IDLE_TIMEOUT_SECONDS=6` 保持不变，方便后续快速回归测试。生命周期配置与 rootfs 构建版本分离，因此修改空闲时间不会重建镜像。Tool Gateway 已支持通过 `--activity-file` 在成功转发模型请求后刷新同一活动时间戳。

### M8 验收结果

已使用 6 秒缩短阈值在当前 ARM64 嵌套 KVM 环境中实际通过：

```text
INSTANCE_HEARTBEAT_EXTENDED id=lifecycle-smoke timeout=6 busy=protected heartbeat=extended
INSTANCE_REAPED id=lifecycle-smoke reason=idle_timeout idle_seconds=6 stop=sigterm volume=preserved
INSTANCE_IDLE_REAP_READY id=lifecycle-smoke timeout=6s ephemeral=cleaned volume=preserved
INSTANCE_RESTART_READY id=lifecycle-smoke data=preserved stop=clean
INSTANCE_DESTROYED id=lifecycle-smoke volume=deleted
PASS: lifecycle manager extended active sessions, reaped idle compute, and preserved restart data
LIFECYCLE_POLICY_READY default_idle=6s smoke_idle=6s busy=protected heartbeat=extended ephemeral=cleaned restart_data=preserved explicit_destroy=verified
```

验收时的 `default_idle=6s` 是当时专门设置的首次测试值；验收后仓库正式默认值已提升为 `300s`。`stop=sigterm` 表示管理器先尝试 guest 关机请求，目标环境未在等待窗口内退出，随后按既定顺序使用 SIGTERM 完成关闭；数据盘经过文件系统检查并在第二次启动中读取到同一随机标记。

## M9 DeepSeek 真实端到端联调

此阶段会产生真实的 DeepSeek API 请求，可能产生少量费用。不要把 API Key 写入仓库根目录的 `.env`，也不要粘贴到聊天、命令参数或 Git 提交中。

macOS 用户在仓库根目录先运行：

```bash
./run.sh configure-deepseek
```

脚本会在本地隐藏输入，通过 SSH 的标准输入传给 Ubuntu，并保存为 Ubuntu 普通用户私有文件：

```text
~/.config/ai-agent-platform/deepseek-api-key
```

该文件权限固定为 `0600`，不会被 `sync` 复制回 macOS，也不位于 Git 仓库内。测试启动时，root 只把它临时复制成 `agent-gateway` 专用用户拥有的 `0400` 文件；测试退出后删除临时副本，保留用户私有原件供下次测试使用。

随后运行：

```bash
./run.sh deepseek-e2e-test
```

Windows/VMware 路径在 Ubuntu VM 内使用等价命令：

```bash
./run-linux.sh configure-deepseek
./run-linux.sh deepseek-e2e-test
```

测试会让 microVM 中的 Pi Agent 使用 `deepseek-flash`，但 Pi 配置里只有无效占位凭据。Pi 只能访问 TAP 地址上的 Tool Gateway，Gateway 在宿主侧注入真实密钥并访问 `https://api.deepseek.com`。Agent 必须调用唯一开放的 `read` 工具读取随机文件，再返回精确标记；同时还会验证 microVM 不能绕过 Gateway 直连 DeepSeek、凭据权限正确，且控制台、配置和审计日志中没有真实密钥。

成功标志：

```text
PASS: Pi Agent used DeepSeek through the credential-isolating Tool Gateway
AGENT_DEEPSEEK_READY model=deepseek-flash tool=read ... credential=placeholder direct_provider=denied
DEEPSEEK_E2E_READY model=deepseek-flash ... credential=host-only:0400 direct_provider=denied audit=redacted tool_call=read
```

### M9 验收结果

已在当前 Apple M4、UTM Ubuntu ARM64 与嵌套 KVM 环境中使用真实 DeepSeek API 完成验收：

```text
PASS: Pi Agent used DeepSeek through the credential-isolating Tool Gateway
AGENT_DEEPSEEK_READY model=deepseek-flash tool=read response=DEEPSEEK_E2E_OK:<随机标记> credential=placeholder direct_provider=denied
DEEPSEEK_E2E_READY model=deepseek-flash gateway=172.31.252.1:18082 credential=host-only:0400 direct_provider=denied audit=redacted tool_call=read
```

这证明 Pi Agent 能通过自定义 OpenAI-compatible provider 配置调用 DeepSeek，并能完成真实工具调用；供应商密钥只存在于 Ubuntu 宿主侧，microVM 仅持有占位凭据且无法绕过 Gateway 直连供应商。实际随机标记不作为固定测试数据写入文档。

配置格式与接口以 [Pi 自定义模型文档](https://pi.dev/docs/latest/models) 和 [DeepSeek API 文档](https://api-docs.deepseek.com/guides/codex) 为准；仓库仍固定 Pi Agent 版本，升级时必须重新执行全部验收。

## M10 HTTP 控制面

M0–M9 使用终端脚本验证了底层能力；M10 在其上增加一个仅监听 Ubuntu loopback 的 HTTP 控制接口，让后续前端可以通过后端操作实例，而不需要直接执行 smoke-test 脚本。

第一版使用 Python 标准库和 SQLite，不引入额外 Web 框架依赖。控制面负责保存产品级实例状态，`InstanceRuntime` 负责创建数据盘、准备 jail、启动 Firecracker，并复用 M8 的生命周期管理器完成心跳、5 分钟空闲回收、停止和销毁。

当前接口：

```http
POST   /api/instances
GET    /api/instances
GET    /api/instances/{id}
POST   /api/instances/{id}/start
POST   /api/instances/{id}/heartbeat
POST   /api/instances/{id}/stop
DELETE /api/instances/{id}
GET    /healthz
```

出于安全考虑，M10 尚未实现用户认证，因此服务拒绝监听 `0.0.0.0`，只能绑定 `127.0.0.1`/`::1`。在认证和授权完成前，不得将这个 root 权限控制接口暴露到局域网或互联网。

运行真实验收：

```bash
# macOS
./run.sh control-plane-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh control-plane-smoke-test
```

测试会通过 API 完成 `created → running → stopped → destroyed`，验证运行状态、心跳、停止时数据盘保留、销毁时数据盘删除，以及未认证接口只监听 loopback。成功标志：

```text
PASS: control plane created, started, queried, heartbeated, stopped, and destroyed a real microVM
CONTROL_PLANE_READY bind=127.0.0.1:18090 storage=sqlite runtime=firecracker idle_timeout=300s auth=loopback-only
```

### M10 验收结果

已在当前 Apple M4、UTM Ubuntu ARM64 与嵌套 KVM 环境中通过真实控制面 API 完成验收：

```text
CONTROL_PLANE_INSTANCE_CREATED id=m10-smoke status=created
CONTROL_PLANE_INSTANCE_STARTED id=m10-smoke status=running
CONTROL_PLANE_HEARTBEAT id=m10-smoke status=running
CONTROL_PLANE_INSTANCE_STOPPED id=m10-smoke status=stopped volume=preserved
PASS: control plane created, started, queried, heartbeated, stopped, and destroyed a real microVM
CONTROL_PLANE_READY bind=127.0.0.1:18090 storage=sqlite runtime=firecracker idle_timeout=300s auth=loopback-only
```

这证明 HTTP 控制面能够驱动真实 Firecracker 实例完成创建、启动、状态查询、心跳、停止和显式销毁；普通停止保留独立数据盘，显式销毁删除实例数据，未认证接口不会监听通配地址。用户认证、多租户和前端页面仍未实现。

验收通过后，可以启动长期运行的本地开发服务：

```bash
# macOS
./run.sh control-plane-start
./run.sh control-plane-status

# Windows 的 Ubuntu VM
./run-linux.sh control-plane-start
./run-linux.sh control-plane-status
```

Windows 环境可以直接在 Ubuntu VM 中请求 `http://127.0.0.1:18090`。macOS 上需要另开一个终端建立 SSH 隧道：

```bash
ssh -N -L 18090:127.0.0.1:18090 agentdev@<UTM-Linux-IP>
```

然后在 macOS 访问：

```bash
curl http://127.0.0.1:18090/healthz
curl -X POST -H 'Content-Type: application/json' \
  -d '{"id":"demo-agent"}' \
  http://127.0.0.1:18090/api/instances
curl -X POST http://127.0.0.1:18090/api/instances/demo-agent/start
curl http://127.0.0.1:18090/api/instances/demo-agent
curl -X POST http://127.0.0.1:18090/api/instances/demo-agent/stop
curl -X DELETE http://127.0.0.1:18090/api/instances/demo-agent
```

完成开发后运行 `./run.sh control-plane-stop`；Windows 的 Ubuntu VM 使用 `./run-linux.sh control-plane-stop`。停止控制面不会直接删除数据盘，只有实例的 `DELETE` 接口会执行显式销毁。

## M11 Agent 任务通道

M11 把此前分别验证的控制面、受控 TAP 网络、Tool Gateway、DeepSeek 和 Pi Agent
整合为正式实例路径。启用 DeepSeek 凭据的控制面启动实例时会自动完成：

- 为每个实例稳定派生独立 `/30` 子网、TAP 名和 nftables 表；
- 只允许 guest 访问宿主 TAP 地址上的 Tool Gateway 与任务桥；
- 以低权限 `agent-gateway` 用户运行两个 sidecar，并由生命周期管理器统一回收；
- 将真实 DeepSeek Key 保留在宿主受限文件，数据盘只保存占位凭据；
- 在 guest 中运行单任务串行 Worker，领取任务、调用 Pi，并回传有序事件；
- 任务活动会刷新 5 分钟空闲计时，停止实例仍保留工作区数据。

新增接口：

```http
POST /api/instances/{id}/tasks
GET  /api/instances/{id}/tasks/{task_id}
GET  /api/instances/{id}/tasks/{task_id}/events
```

当前事件接口采用短轮询 JSON；这是 MVP 的可验证边界，后续前端里程碑再升级为
SSE/WebSocket 流式显示。Gateway 当前也仍会缓冲完整上游响应，因此本阶段验证的
是安全、执行和结果链路，而不是逐 token 展示体验。

在已经完成 `configure-deepseek` 的环境执行：

```bash
# macOS
./run.sh agent-task-smoke-test

# Windows 的 Ubuntu VM
./run-linux.sh agent-task-smoke-test
```

测试会先由宿主向工作区写入模型未知的随机标记，重启同一实例，再让 Pi 必须使用
`read` 工具取得内容；随后检查任务事件顺序、Gateway 审计、数据盘持久化以及显式
销毁。因为 token 不出现在 prompt 中，模型无法靠复述指令绕过工具调用。首次执行
可能因 guest Worker 文件变更而重建 rootfs。预期成功标志：

```text
PASS: control plane completed a real Pi Agent task through DeepSeek
AGENT_TASK_READY transport=http-poll events=ordered gateway=isolated persistence=preserved idle_timeout=300s
```

### M11 验收结果

已在当前 Apple M4、UTM Ubuntu ARM64 与嵌套 KVM 环境中完成真实验收：

```text
Starting managed Pi Agent microVM with isolated Gateway and task bridge...
Restarting the managed instance with a host-seeded persistent marker...
PASS: control plane completed a real Pi Agent task through DeepSeek
AGENT_TASK_READY transport=http-poll events=ordered gateway=isolated persistence=preserved idle_timeout=300s
```

这证明控制面能够向真实 microVM 内的 Pi Agent 下发任务；Pi 只能通过宿主 Tool
Gateway 使用 DeepSeek，并能读取 prompt 中未知的工作区随机标记。停止并重启计算
实例后，同一数据盘内容仍然保留，任务事件按 `queued → started → completed` 顺序
记录，正式空闲回收阈值保持为 5 分钟。

## M12 Web 管理页面

M12 在 M10/M11 API 上增加一个 React + TypeScript 页面，代码位于 `apps/web/`。
它没有绕过现有安全边界：浏览器只连接本机 Vite 代理，代理再访问 loopback 控制面；
DeepSeek Key 仍只保存在 Ubuntu 宿主的受限文件中，不会进入浏览器、仓库或 microVM。

当前页面支持：

- 查看、创建、启动、停止和显式销毁 Agent 实例；
- 查看 microVM 进程状态、最后更新时间和 5 分钟空闲回收倒计时；
- 向运行中的 Pi Agent 提交任务，并每秒轮询任务状态和最终结果；
- 清晰显示控制面离线、实例错误和任务失败信息；
- 窄屏自适应布局，便于演示和后续继续开发。

### macOS 真实链路验收

以下三个命令分别使用三个终端。`start` 返回后不需要保持终端占用：

```bash
# 终端 1
./run.sh start

# 终端 2：保持运行
./run.sh tunnel

# 终端 3：保持运行
./run.sh web-dev
```

打开 `http://127.0.0.1:5173`，页面右上角应显示“控制面在线”。随后依次：

1. 创建实例；
2. 点击“启动实例”，等待状态变为“运行中”；
3. 输入一个小任务并发送，等待任务显示“已完成”和 DeepSeek 返回内容；
4. 点击“停止”验证数据保留；仅在确认不再需要数据时点击“销毁”。

前端的独立构建检查可随时运行：

```bash
./run.sh web-build
```

### Windows + VMware 真实链路验收

控制面和页面都在 Ubuntu VM 内运行，因此 Vite 代理无需跨虚拟机访问 M10 API：

```bash
# Ubuntu 终端 1
./run-linux.sh start

# Ubuntu 终端 2：保持运行
./run-linux.sh web-dev
```

在 Windows PowerShell 运行下面的转发，并保持窗口打开：

```powershell
ssh -N -L 5173:127.0.0.1:5173 agentdev@<Ubuntu-VM-IP>
```

然后用 Windows 浏览器打开 `http://127.0.0.1:5173`，按与 macOS 相同的四步进行
验收。Linux 入口会从 rootfs 下载缓存复用固定 Node.js 22.19.0；缓存不存在时会从
Node.js 官方地址下载并校验 SHA-256，因此不依赖 Ubuntu 自带 Node 版本。

M12 当前仍沿用 M10 的 loopback-only 无认证边界，不得把 5173 或 18090 改为
`0.0.0.0` 暴露到局域网。用户登录、多租户授权、SSE/WebSocket 流式输出和生产静态
部署属于后续里程碑，不是本阶段的安全承诺。

### M12 验收结果

已在当前 Apple M4、UTM Ubuntu ARM64 与嵌套 KVM 环境中通过页面创建并启动真实
实例，向 Pi Agent 提交问题，并成功取得 DeepSeek 回答。页面与 API 之间通过 SSH
loopback 隧道连接，模型凭据仍由宿主 Tool Gateway 隔离。空闲倒计时只把真实任务
领取、模型请求和事件回传计为活动；guest 对空任务队列的内部轮询不会延长实例寿命。
已继续等待完整 5 分钟并确认实例自动停止，独立数据盘保持不变，可重新启动恢复工作区。

## 跨平台常见错误

| 平台 | 现象 | 最可能原因 | 处理 |
|---|---|---|---|
| macOS | `utmctl` 报参数连在一起 | 多条命令粘贴时缺少换行 | 每条命令单独执行，或确认命令之间有换行 |
| macOS | SSH 卡住或连接超时 | VM IP 变化、OpenSSH 未安装或 VM 网络异常 | 在 Ubuntu 重跑 `hostname -I`，用 `ssh 用户名@IP` 单独验证，再执行 `./run.sh configure` |
| macOS | 脚本反复要求密码 | 当前使用 SSH 密码和远程 `sudo`，属于预期行为 | MVP 阶段可继续输入；后续可配置 SSH Key 和受限 sudo 规则 |
| Windows | VMware 无法启用嵌套虚拟化 | Hyper-V、VBS 或内存完整性占用 VT-x/AMD-V | 公司设备先联系 IT，再按 VMware/Broadcom 官方文档排查 |
| 两者 | `/dev/kvm` 不存在 | 外层虚拟机未暴露嵌套虚拟化，或 BIOS/虚拟化引擎配置不正确 | 停止项目脚本，回到 UTM/VMware 与 BIOS 设置检查 |
| 两者 | `/dev/kvm` 存在但权限失败 | 当前用户不在 `kvm` 组，或新组尚未生效 | `sudo usermod -aG kvm "$USER"`，完整注销并重新登录 |
| Windows | `uname -m` 输出 `aarch64` | ISO/设备架构选择错误 | 当前 Windows 路径只支持 x86_64 Windows + Ubuntu AMD64 |
| 两者 | hard link 或 cross-device 错误 | jail、镜像或数据盘跨文件系统/位于共享目录 | 使用 Ubuntu VM 原生 ext4 磁盘，不使用 UTM/VMware 共享目录 |
| 两者 | 出现大量 `I: Retrieving ...` | rootfs 构建指纹变化，正在重新构建最小 Ubuntu | 等待本次完成；指纹不变时下次会显示 `Verified existing runtime rootfs` |
| 两者 | Ubuntu snapshot 单包下载失败 | 官方快照服务瞬时失败 | 脚本会自动重试三次；最终失败后重新运行同一命令 |
| 两者 | DeepSeek 返回 401/403 | API Key 无效、过期或账户权限不足 | 重新运行 `configure-deepseek`，不要把 Key 写进 `.env` 或命令参数 |
| 两者 | `Unknown command` | 本地仓库或同步到 VM 的代码版本过旧 | `git pull` 后重试；macOS 路径会在命令开始时自动 `sync` |

Windows 首台试点设备应保存以下信息和每个阶段最后的 `PASS`/`*_READY` 行：

```bash
uname -a
cat /etc/os-release
ls -l /dev/kvm
groups
sudo dmesg | grep -Ei 'kvm|vmx|svm|virtualization' | tail -n 50
```

全部通过后，才能把 Windows 支持状态从“待实机验收”更新为“已验证”。

## 可配置参数

脚本不包含个人用户名、个人目录或固定 VM 地址。以下参数保存在本机 `.env` 中，并已被 `.gitignore` 排除：

```dotenv
VM_HOST=<UTM-Linux-IP>
VM_USER=agentdev
REMOTE_DIR=/opt/ai-agent-platform
UTM_APP_PATH=/Applications/UTM.app
```

- `VM_HOST`：UTM Linux 的 IP 或主机名。
- `VM_USER`：Linux 登录用户名。
- `REMOTE_DIR`：仓库同步到 Linux 的位置。
- `UTM_APP_PATH`：UTM 不在标准位置时可以修改。

Firecracker、测试内核、Node.js 和 Pi Agent 的版本及校验值位于 `config/versions.env`，通过代码审查更新，不使用浮动的 `latest`。

## 文件与数据边界

- Git 仓库保存源代码、配置模板、构建脚本和测试。
- `.env` 只保存在开发者本机，不提交。
- DeepSeek API Key 只保存在 Linux 用户的 `~/.config/ai-agent-platform/`，不写入 `.env`、仓库或 microVM。
- Firecracker 内核、rootfs、数据盘、日志和运行状态不提交到 Git。
- 模型密钥、SSH 私钥和 Pi Agent 登录凭据禁止进入仓库和 microVM 镜像。
- Firecracker 镜像与数据盘必须存放在 Linux VM 的原生文件系统中，不能放在 macOS/UTM 或 Windows/VMware 共享目录。

## 可复现性约束

- 所有依赖版本必须固定。
- 下载的二进制、内核和软件包必须使用发布方提供的 SHA-256 或 SHA-512 校验值验证。
- Linux 安装脚本必须可重复执行。
- 不允许将开发者姓名、macOS 用户目录、个人 IP 或本机绝对项目路径写入共享脚本。
- 每完成一个可用里程碑，同步更新本 README 的进度、命令和支持范围。
