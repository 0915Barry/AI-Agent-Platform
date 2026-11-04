# 单机 MVP 架构设计（Linux + KVM）

> 本文描述当前仓库在一台 Linux Host 上的真实部署结构。macOS + UTM 和
> Windows + VMware 只是开发环境的外层虚拟化；正式服务器直接从 Linux Host 开始。

## 一、单机分层

```text
客户端层
  agentctl / 上层后端 / 未来前端
         │ HTTP + SSE
         ▼
应用控制层
  Control Plane :18090（仅 loopback）
  ├─ Auth / Workspace / Conversation / Task
  ├─ SQLite control-plane.db
  └─ AgentRuntimeAdapter
         │
         ├──────────────► PiRuntimeAdapter → PiGateway
         │                              │ TCP/JSONL
         ▼                              ▼
运行编排层                         Guest Pi RPC :8080
  InstanceRuntime                   socat → pi-rpc-session → Pi
  ├─ NetworkManager
  ├─ RuntimeStore
  ├─ Lifecycle
  └─ Tool Gateway
         │
         ▼
虚拟化层
  Jailer → Firecracker → /dev/kvm
         │
         ▼
  microVM（独立Guest Linux）
```

## 二、Host真实目录

```text
/srv/fc/
├── artifacts/
│   ├── vmlinux-5.10.268
│   ├── agent-rootfs.ext4
│   ├── agent-rootfs.build-id
│   └── agent-rootfs.manifest
├── volumes/
│   └── users/
│       └── <user-id>.ext4
└── snapshots/
    └── instances/
        └── <instance-id>/
            ├── memory.snap
            ├── vmstate.snap
            └── manifest.json

/srv/jailer/firecracker/<instance-id>/root/
├── vmlinux
├── rootfs.ext4
├── data.ext4
├── config.json
├── firecracker.socket
└── snapshot/                 # 仅快照恢复时存在

/var/lib/fc/
├── control-plane.db
├── control-plane.db-wal
├── control-plane.db-shm
├── instances/                # 历史兼容用的实例JSON元数据
├── activity/                 # 实例活动时间文件
└── control-plane/
    └── <instance-id>/
        ├── console.log
        ├── reaper.log
        └── sidecars/          # 模型网关日志、审计和临时凭据
```

内核、rootfs和数据盘通过硬链接进入Jailer chroot，因此`/srv/fc`与`/srv/jailer`
必须位于同一个Linux文件系统。SQLite取代了新版实例的JSON运行元数据和JSON网络
租约；旧JSON文件只保留兼容导入或清理能力。

## 三、数据盘视图

Host保存的是一个ext4镜像文件：

```text
/srv/fc/volumes/users/<user-id>.ext4
```

Firecracker把它注册为Guest第二块虚拟磁盘`/dev/vdb`。Guest初始化脚本执行：

```text
/dev/vdb
  └─ mount → /mnt/agent-data
                  ├─ workspace ── bind → /workspace
                  └─ pi-state  ── bind → /home/pi/.pi
```

一个用户只有一块稳定数据盘。多个Workspace只是盘内不同目录，不是多块数据盘。
多个Conversation的Pi原生Session也保存在同一数据盘的`pi-state/agent/sessions/`。

## 四、启动一台受管实例

```text
客户端 POST /api/instances/<id>/start
        │
        ▼
Control Plane
  校验认证、属主、单实例配额与状态迁移
        │
        ▼
InstanceRuntime
  ① 确保Host依赖、数据盘和凭据存在
  ② e2fsck检查数据盘
  ③ NetworkManager申请/30租约
  ④ 创建TAP、路由和nftables
  ⑤ 写入数据盘中的Pi provider占位配置
  ⑥ 启动Tool Gateway
  ⑦ 准备Jailer chroot及config.json
  ⑧ Jailer启动Firecracker
        │
        ▼
Guest
  ⑨ 挂载/dev/vdb并bind Workspace和Pi状态目录
  ⑩ 配置eth0、默认路由和DNS
  ⑪ socat监听Guest IP:8080
  ⑫ 输出AGENT_RUNTIME_READY和PI_RPC_READY
        │
        ▼
Host登记SQLite运行元数据，启动空闲回收watcher
```

当前机器规格为2 vCPU、1024 MiB内存、4 GiB共享只读rootfs和1 GiB用户数据盘。

## 五、一次对话请求

```text
agentctl / 上层应用
  │ POST消息
  ▼
Control Plane
  │ SQLite写入用户消息与queued Task
  │ 自动启动已停止的实例
  ▼
AgentRuntimeAdapter
  ▼
PiRuntimeAdapter / PiGateway
  │ TCP连接Guest:8080
  ▼
socat → pi-rpc-session.sh → pi --mode rpc
  │ Pi通过Tool Gateway调用DeepSeek
  │ Pi RPC持续输出JSONL事件
  ▼
PiGateway转换为平台统一事件
  ▼
TaskStore写入SQLite，并通过SSE流式返回客户端
```

当前每条TCP连接会启动一个Pi进程，连接结束后Pi退出。打开旧会话时优先从SQLite
消息缓存读取，不需要为了展示历史而启动Pi。真实发送消息仍会启动Pi并让它从数据盘
加载对应的原生Session。

## 六、网络设计

地址池为`172.29.0.0/16`，每个运行实例分配一个`/30`槽位：

```text
子网：172.29.X.Y/30
Host TAP网关：第一个可用地址
Guest eth0：第二个可用地址
```

NetworkManager负责：

- 在SQLite中原子预留租约；
- 创建TAP并配置Host IP；
- 开启必要路由与IPv4 forwarding；
- 创建实例独立nftables表；
- Guest就绪后将租约改为active；
- 停止、失败或销毁时清理规则、TAP和租约；
- 快照恢复时尝试重新占用原网络槽位。

## 七、安全边界

### 7.1 microVM隔离

Guest拥有独立Linux内核、vCPU、内存和虚拟设备。Agent即使获得Guest内权限，也不
等同于获得Linux Host权限。

### 7.2 Jailer隔离

Firecracker运行在独立chroot中，使用低权限UID/GID、cgroup v2、`nofile=2048`、
capability收缩和seccomp过滤。chroot中只放入本实例所需文件及设备。

### 7.3 rootfs与用户身份

共享rootfs只读，Guest中的Pi以UID/GID 1001运行。可写范围主要来自用户数据盘映射
出的`/workspace`与`/home/pi/.pi`。

### 7.4 凭据隔离

DeepSeek Key不进入rootfs、数据盘、Guest内核参数或Pi Session。Host Tool Gateway
读取权限受限的临时凭据文件，向上游注入真实Key，并对审计内容脱敏。

## 八、状态、停止与快照

正常停止会终止Firecracker和sidecar，删除Jailer、TAP及nftables资源，释放网络租约，
但保留用户数据盘与业务数据库记录。

销毁受认证用户的实例时，当前产品语义仍保留其稳定用户数据卷；无属主临时测试实例
可连同临时数据盘删除。

暂停流程使用Firecracker原生全量快照：

```text
running → suspending → suspended
```

快照保存Guest内存与虚拟机状态，不包含用户数据盘正文。恢复时验证owner、rootfs
Build ID和数据盘路径，重新创建Host资源后加载快照：

```text
suspended → resuming → running
```

## 九、当前实现边界

已实现：认证、单用户单实例、Workspace、Conversation、Task、SQLite消息缓存、统一
Runtime Adapter、Pi RPC、模型网关、SSE、网络租约、空闲回收、异常恢复和手动快照。

未实现：常驻Pi、多Agent动态切换、MCP Gateway、动态Skill、向量知识库、多Host
调度、对象存储数据卷同步和正式Web前端。
