# Windows 开发环境搭建

> 支持级别：仓库已经支持在 `x86_64` Ubuntu 中构建对应架构的 Firecracker、内核、rootfs、Node.js 和 Pi Agent；Windows 外层虚拟化尚未在项目组的真实 Windows 设备上完成验收。请先选择一位同事执行本页的“试点验收”，通过后再推广。

## 1. 先明确平台边界

Firecracker 只能运行在提供 KVM 的 Linux 环境中，不能直接运行在 Windows 上。Windows 在本项目中只是最外层宿主机：

```text
Windows x86_64
└── VMware Workstation
    └── Ubuntu Server 24.04 x86_64（必须获得 /dev/kvm）
        └── Firecracker
            └── Agent microVM
```

- 不使用 UTM。UTM 官方将其定位为 Apple 平台应用，并不是 Windows 宿主机方案。
- 不把 WSL2 当作 Firecracker 宿主。本文也不要求安装 WSL、Git Bash 或 Windows 版 rsync。
- 不推荐使用 Hyper-V VM 承载 Firecracker。微软说明 Hyper-V 虚拟机内的非 Microsoft 虚拟化程序不属于受支持场景。
- 如果公司策略不允许关闭 Hyper-V/VBS，或者 VMware 无法向 Ubuntu 暴露虚拟化扩展，请改用一台裸机或远程 Linux KVM 服务器；后续 Ubuntu 内命令完全相同。

官方依据：

- [Firecracker Getting Started：需要 Linux KVM，支持 x86_64 与 aarch64](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)
- [UTM：为 macOS 和 Apple 平台设计](https://mac.getutm.app/)
- [Microsoft：Hyper-V Nested Virtualization 的非 Microsoft 虚拟化限制](https://learn.microsoft.com/en-us/virtualization/hyper-v-on-windows/user-guide/nested-virtualization)
- [Broadcom：VMware 嵌套虚拟化与 Hyper-V/VBS 冲突排查](https://knowledge.broadcom.com/external/article/389469/virtualized-intel-vtxept-is-not-supporte.html)

## 2. Windows 与硬件要求

本地 VMware 试点要求：

- x86_64 的 Windows 10/11 电脑，不支持 Windows on ARM。
- Intel VT-x/EPT 或 AMD-V/RVI 已在 BIOS/UEFI 中开启。
- 建议宿主机至少 16 GB 内存；Ubuntu VM 分配 4 个 vCPU、8 GB 内存和 80 GB 磁盘。
- 安装 VMware Workstation，并获得修改虚拟机 CPU 设置的权限。
- Ubuntu 使用 `ubuntu-24.04.x-live-server-amd64.iso`，不要下载 ARM64 ISO。

先在 Windows 的“任务管理器 → 性能 → CPU”中确认“虚拟化：已启用”。也可以在命令提示符运行：

```powershell
systeminfo.exe
```

如果 BIOS/UEFI 没有开启虚拟化，先停止，不要继续安装。

## 3. 创建 VMware Ubuntu VM

1. 创建 Ubuntu 24.04 64-bit 虚拟机，建议使用 NAT 网络。
2. 完全关闭虚拟机，不要处于暂停状态。
3. 打开 `VM Settings → Hardware → Processors`。
4. 勾选 `Virtualize Intel VT-x/EPT or AMD-V/RVI`。
5. 分配 4 个 vCPU、8 GB 内存和 80 GB 虚拟磁盘。
6. 使用 AMD64 Ubuntu Server ISO 安装系统；普通服务器安装即可，不需要 microk8s、Docker 或其他 Featured Server Snaps。
7. 如需从 Windows 终端连接，安装器中勾选 OpenSSH Server；只使用 VMware 控制台则不是必需项。

如果 VMware 启动时报错：

```text
Virtualized Intel VT-x/EPT is not supported on this platform
```

通常表示 Hyper-V、VBS 或“内存完整性”正在占用硬件虚拟化能力。不要直接修改公司电脑的安全策略；先征得 IT 同意，再严格按上面的 Broadcom 官方排查文档处理并重启。关闭这些功能会降低部分 Windows 安全保护，同时也会导致 WSL2、Windows Sandbox 等依赖项不可用。

## 4. 在 Ubuntu 中确认 KVM

登录 Ubuntu，执行：

```bash
uname -m
grep -Eoc '(vmx|svm)' /proc/cpuinfo
ls -l /dev/kvm
groups
```

正确结果应满足：

- `uname -m` 输出 `x86_64`。
- `vmx`/`svm` 计数大于 `0`。
- `/dev/kvm` 存在。
- 当前用户可以读写 `/dev/kvm`。

如果 `/dev/kvm` 存在但当前用户不在 `kvm` 组：

```bash
sudo usermod -aG kvm "$USER"
```

随后必须完整退出 Ubuntu 登录会话并重新登录，再执行：

```bash
test -r /dev/kvm && test -w /dev/kvm && echo 'KVM access OK'
```

如果 `/dev/kvm` 不存在，先回到 VMware 检查嵌套虚拟化选项；不要继续运行项目脚本。

## 5. 在 Ubuntu 原生磁盘中克隆仓库

仓库、Firecracker 镜像和数据盘必须位于 Ubuntu 的 ext4 文件系统。不要将仓库放在 VMware Shared Folders、Windows SMB 共享或 `/mnt` 下，否则 hard link、权限或文件锁可能不符合脚本要求。

在 Ubuntu 中运行：

```bash
sudo apt-get update
sudo apt-get install -y git
git clone <repository-url> AI-Agent-Platform
cd AI-Agent-Platform
chmod +x run-linux.sh scripts/linux/*.sh scripts/guest/*.sh
```

通过 Git 克隆时通常会保留可执行位，`chmod` 是为了兼容 ZIP 解压或错误的 Git 文件模式配置。

Windows 同事使用的是 `run-linux.sh`，不要在 Ubuntu VM 中运行面向 Mac 控制端的 `run.sh`。

## 6. 逐步验收

先只检查环境：

```bash
./run-linux.sh doctor
```

只有出现下面两行才继续：

```text
KVM access: OK
Linux/KVM checks passed
```

安装 Firecracker 并启动最小 microVM：

```bash
./run-linux.sh setup
./run-linux.sh microvm-smoke-test
```

然后按顺序运行：

```bash
./run-linux.sh runtime-smoke-test
./run-linux.sh security-smoke-test
./run-linux.sh network-smoke-test
./run-linux.sh persistence-smoke-test
./run-linux.sh gateway-smoke-test
./run-linux.sh lifecycle-smoke-test
./run-linux.sh configure-deepseek
./run-linux.sh deepseek-e2e-test
```

不要在第一条失败后继续执行后面的命令。第一次构建 rootfs 会下载 Ubuntu、Node.js 与 Pi Agent，耗时较长；后续相同构建会复用缓存。

## 7. 试点验收记录

Windows/x86_64 尚未由当前维护者实机执行，因此第一位 Windows 同事需要保存以下信息：

```bash
uname -a
cat /etc/os-release
ls -l /dev/kvm
groups
sudo dmesg | grep -Ei 'kvm|vmx|svm|virtualization' | tail -n 50
```

并保存每个 `run-linux.sh` 命令最后的 `PASS` 或 `*_READY` 行。全部通过后，再把 Windows 支持状态从“待实机验收”改为“已验证”。

## 8. 常见错误

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| VMware 无法启用嵌套虚拟化 | Hyper-V/VBS/内存完整性占用 VT-x/AMD-V | 按 Broadcom 官方文档检查；公司设备先联系 IT |
| `/dev/kvm` 不存在 | VMware 未勾选嵌套虚拟化或 BIOS 未开启虚拟化 | 关闭 VM 后检查 CPU 设置和 BIOS |
| `/dev/kvm` 存在但 doctor 报权限错误 | 用户组尚未生效 | 加入 `kvm` 组后完整注销并重新登录 |
| 脚本显示 `aarch64` | 下载了 ARM64 Ubuntu 或使用 Windows ARM 设备 | 本指南只支持 Windows x86_64 + Ubuntu AMD64 |
| hard link / cross-device 错误 | `/srv/fc/volumes` 与 `/srv/jailer` 不在同一 Linux 文件系统 | 使用 Ubuntu VM 原生磁盘，不使用共享目录 |
| Ubuntu snapshot 单包下载失败 | 快照服务瞬时失败 | 让当前命令结束；若三次自动重试后仍失败，再重跑同一命令 |
