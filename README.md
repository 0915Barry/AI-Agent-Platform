# AI Agent Platform MVP

在隔离的 Firecracker microVM 中运行 Pi Agent 的企业智能体平台 MVP。

当前仓库按“先脚本化、再执行、最后验收”的方式推进。macOS 开发者在宿主机运行 `run.sh`，脚本通过 SSH 操作 UTM Linux；Windows 开发者在 VMware Ubuntu VM 内运行 `run-linux.sh`，避免依赖不一致的 Windows Shell 工具。

## 项目文档

规划、架构和部署设计统一存放在 [`docs/`](./docs/README.md)：

- [企划书](./docs/企划书.md)
- [架构与实现方案汇报](./docs/架构与实现方案汇报.md)
- [服务器单机 MVP 架构设计图](./docs/服务器单机MVP架构设计图.md)
- [Windows 环境搭建与排错](./docs/Windows环境搭建.md)

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

Windows 不使用 UTM；UTM 官方只面向 Apple 平台。Windows 也不直接运行 Firecracker，Firecracker 始终运行在 Linux KVM 环境中。详细的兼容性边界、公司设备安全注意事项和逐步命令见 [Windows 环境搭建](./docs/Windows环境搭建.md)。在第一台真实 Windows 设备完成全部测试前，不把 Windows 路径标记为正式支持。

## macOS 新环境首次使用

先在 UTM 中安装 ARM64 Ubuntu，并安装 OpenSSH Server。在 Ubuntu 中运行 `hostname -I` 获取 VM 地址。

然后在 macOS 的仓库根目录运行：

```bash
./run.sh setup <UTM-Linux-IP> [Linux用户名]
```

如果安装时使用了推荐用户名 `agentdev`，可以省略用户名：

```bash
./run.sh setup <UTM-Linux-IP>
```

`setup` 会自动完成：

1. 保存本机连接配置到被 Git 忽略的 `.env`。
2. 检查 macOS、CPU 架构和 UTM。
3. 通过 SSH 检查 Linux、架构和 `/dev/kvm`。
4. 同步仓库到 Linux VM。
5. 安装仓库固定版本的 Firecracker 和 jailer。

安装完成后运行最小 microVM 验收：

```bash
./run.sh microvm-smoke-test
```

成功标志：

```text
PASS: Firecracker booted the aarch64 microVM and reached guest init
```

## Windows 新环境首次使用

推荐让 Windows 只作为 VMware 宿主，把 Git 仓库和所有命令放进 Ubuntu VM 的原生 Linux 文件系统。这样不依赖 WSL、Git Bash、Windows `rsync` 或未经实机验证的 PowerShell 包装层。

在 Ubuntu VM 中克隆仓库后使用：

```bash
./run-linux.sh doctor
./run-linux.sh setup
./run-linux.sh microvm-smoke-test
```

确认最小 microVM 通过后，再按照 [Windows 环境搭建](./docs/Windows环境搭建.md) 给出的顺序运行后续测试。Windows 常见的 Ubuntu VM 是 `x86_64`，脚本会自动选择 AMD64 Ubuntu、Node.js x64 以及经过 SHA-256 校验的 x86_64 Firecracker CI 内核和 initramfs；不会下载 ARM64 构建物。

## macOS 控制端命令

```bash
./run.sh doctor
./run.sh configure <UTM-Linux-IP> [Linux用户名]
./run.sh vm-doctor
./run.sh sync
./run.sh install-firecracker
./run.sh prepare-microvm
./run.sh microvm-smoke-test
./run.sh build-rootfs
./run.sh runtime-smoke-test
./run.sh security-smoke-test
./run.sh network-smoke-test
./run.sh persistence-smoke-test
./run.sh gateway-smoke-test
./run.sh lifecycle-smoke-test
./run.sh configure-deepseek
./run.sh deepseek-e2e-test
./run.sh setup <UTM-Linux-IP> [Linux用户名]
```

| 命令 | 作用 |
|---|---|
| `doctor` | 检查 macOS、ARM64 和 UTM |
| `configure` | 生成本机 `.env`，不提交到 Git |
| `vm-doctor` | 通过 SSH 检查 Linux 和 KVM |
| `sync` | 将源代码同步到 Linux VM 的部署目录 |
| `install-firecracker` | 幂等安装固定版本的 Firecracker |
| `prepare-microvm` | 下载并校验与 Linux VM CPU 架构匹配的测试内核和 initramfs |
| `microvm-smoke-test` | 启动最小 microVM 并验证 guest init |
| `build-rootfs` | 构建固定版本的 Ubuntu、Node.js 与 Pi Agent rootfs |
| `runtime-smoke-test` | 启动正式 rootfs，验证 Node、Pi Agent 与非 root 用户 |
| `security-smoke-test` | 验证实例内权限和 Firecracker jailer 加固 |
| `network-smoke-test` | 验证 TAP、HTTPS 出站和私有网络默认拒绝 |
| `persistence-smoke-test` | 验证只读系统盘和跨 microVM 重建的数据持久化 |
| `gateway-smoke-test` | 验证宿主侧凭据注入、路由白名单、脱敏审计和上游隔离 |
| `lifecycle-smoke-test` | 以 6 秒阈值验证心跳续期、长任务保护、空闲回收、重启和显式销毁 |
| `configure-deepseek` | 隐藏输入并将 DeepSeek API Key 保存到 Linux 用户私有配置目录，不进入仓库 |
| `deepseek-e2e-test` | 让 Pi Agent 通过 Tool Gateway 调用 DeepSeek 并完成真实 `read` 工具调用 |
| `setup` | 完成首次配置、检查、同步与 Firecracker 安装 |

Ubuntu VM 内的等价入口是 `./run-linux.sh <command>`。它不进行 SSH 和仓库同步，适合 Windows 同事直接在 Ubuntu VM 中使用；支持的命令可运行 `./run-linux.sh help` 查看。

## Agent 运行镜像

最小 microVM 验收通过后，在 macOS 仓库根目录运行：

```bash
./run.sh runtime-smoke-test
```

该命令会在 UTM Linux 中完成构建并启动验收。第一次需要下载 Ubuntu 包、Node.js 和 Pi Agent，耗时会明显长于最小启动测试；相同构建指纹再次执行时会复用已验证的镜像。Ubuntu 系统包来自配置文件中固定日期的官方快照，构建完成后还会保存实际安装的软件包清单。

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

## 下一里程碑：权限与 jailer

在接入网络前先完成两层安全验收：

1. microVM 内部：系统运行时归 `root:root`，`pi` 只能写 `/workspace` 与 `/home/pi`，无法读取 `/etc/shadow` 或覆盖 Node/Pi。
2. UTM Linux 上：使用专用 `firecracker` 用户和 jailer 启动 VMM，验证 chroot、seccomp、零 capabilities、空环境变量、文件描述符上限和设备访问范围。

“隔离”指实例无法访问宿主和其他租户资源，不要求实例无法识别自己运行在虚拟化环境中。

在 macOS 仓库根目录运行：

```bash
./run.sh security-smoke-test
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

在 macOS 仓库根目录运行：

```bash
./run.sh network-smoke-test
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

在 macOS 仓库根目录运行：

```bash
./run.sh persistence-smoke-test
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

在 macOS 仓库根目录运行：

```bash
./run.sh gateway-smoke-test
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

第一次验收在 macOS 仓库根目录运行：

```bash
./run.sh lifecycle-smoke-test
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
