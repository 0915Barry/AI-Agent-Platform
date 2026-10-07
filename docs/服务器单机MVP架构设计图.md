# 单机 MVP 架构设计图（Linux + KVM 直装版）

> 场景：**租一台支持 KVM 的 Linux 服务器，从零搭建**，不迁移本地 Lima 环境
> 目标：跑通「一条命令拉起实例 → Agent 干活 → 空闲回收 → 数据保留」的完整链路
> 说明：本图是**目标形态**，本地 Lima 那层（开发脚手架）**在服务器上不存在**

---

## 一、总体分层图

```
╔══════════════════════════════════════════════════════════════════════════╗
║                          公网 / 企业内网                                  ║
║                    （用户终端 · 模型服务 API）                            ║
╚════════════════════════════════╤═════════════════════════════════════════╝
                                 │
                    ┌────────────▼────────────┐
                    │   eth0（物理网卡）        │
                    │   公网 IP + 内网 IP       │
                    └────────────┬────────────┘
                                 │
╔════════════════════════════════╪═════════════════════════════════════════╗
║  L0  物理服务器（租用 · 支持 KVM）                                        ║
║      CPU 支持 VT-x/AMD-V · /dev/kvm 存在 · Ubuntu 22.04/24.04            ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  L5  治理层 · Tool Gateway                          [待搭建]        │  ║
║  │      凭据注入 · 出站审计 · 活动记账                                  │  ║
║  │      监听 内网地址:PORT（★ 不绑 0.0.0.0）                           │  ║
║  │      真实凭据存 /etc/fc/gateway.env（600，不进实例）                 │  ║
║  └────────────────────────────────────────────────────────────────────┘  ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  L6  生命周期层 · 控制脚本                          [已搭建]        │  ║
║  │      fc-up.sh       一条命令拉起实例（建 chroot→接盘→启 VMM）       │  ║
║  │      fc-down.sh     优雅关闭（sync→SIGTERM→保留 chroot）            │  ║
║  │      reaper.sh      空闲回收器（扫 activity/ 目录）                 │  ║
║  │      statusd        状态端口（HTTP 查询 / JSON / 实时流）            │  ║
║  └────────────────────────────────────────────────────────────────────┘  ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  L4  网络层 · 主机侧网络                           [待搭建]        │  ║
║  │                                                                    │  ║
║  │    br-fc（Linux bridge）                                            │  ║
║  │      ├── tap-pi01   ← 实例1 的虚拟网卡                              │  ║
║  │      ├── tap-pi02   ← 实例2                                        │  ║
║  │      └── ...                                                       │  ║
║  │                                                                    │  ║
║  │    iptables:                                                       │  ║
║  │      -A FORWARD -i tap+ -o eth0 -j ACCEPT          ← 允许出公网     │  ║
║  │      -A FORWARD -i eth0 -o tap+ -m state \                         │  ║
║  │           --state RELATED,ESTABLISHED -j ACCEPT    ← 允许回包       │  ║
║  │      -P FORWARD DROP                               ← ★ 其余全丢     │  ║
║  │      -t nat -A POSTROUTING -s 10.x.x.0/30 -j MASQUERADE            │  ║
║  │                                                                    │  ║
║  │    每实例独立 /30 网段 → 拓扑层排除邻居关系                          │  ║
║  │    ⚠️ 无 DHCP，由 MAC 反推地址                                      │  ║
║  └────────────────────────────────────────────────────────────────────┘  ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  L2  VMM 层 · Firecracker + jailer                 [待搭建]        │  ║
║  │                                                                    │  ║
║  │    jailer（加固包裹）                                               │  ║
║  │      ├─ 关闭继承 fd · 清空环境变量                                   │  ║
║  │      ├─ pivot_root 进 chroot（★ 仅 8 个可见条目）                    │  ║
║  │      ├─ mknod 仅建 /dev/kvm · /dev/net/tun                         │  ║
║  │      ├─ setrlimit（fd 上限）                                       │  ║
║  │      └─ 降权到 firecracker 用户 → exec VMM                          │  ║
║  │                                                                    │  ║
║  │    firecracker v1.17.0                                             │  ║
║  │      ├─ 自身 seccomp 默认开启（按线程延迟加载）                      │  ║
║  │      ├─ 最小设备模型：virtio-net / block / console / RTC            │  ║
║  │      └─ 持有数据盘的 fd（open 后即有句柄）                          │  ║
║  └────────────────────────────────────────────────────────────────────┘  ║
║                                                                          ║
║  ┌──────────────────────────────┐   ┌──────────────────────────────┐    ║
║  │  L3  隔离单元 · microVM 01    │   │  L3  隔离单元 · microVM 02    │    ║
║  │  ┌────────────────────────┐  │   │  ┌────────────────────────┐  │    ║
║  │  │ 独立内核（Ubuntu 24.04）│  │   │  │ 独立内核（Ubuntu 24.04）│  │    ║
║  │  ├────────────────────────┤  │   │  ├────────────────────────┤  │    ║
║  │  │ 系统资产（属主 root）    │  │   │  │ 系统资产（属主 root）    │  │    ║
║  │  │  Node 22 / Pi 0.87      │  │   │  │  Node 22 / Pi 0.87      │  │    ║
║  │  │  /dev/vda → 只读挂载     │  │   │  │  /dev/vda → 只读挂载     │  │    ║
║  │  ├────────────────────────┤  │   │  ├────────────────────────┤  │    ║
║  │  │ 运行时空间（属主 pi）    │  │   │  │ 运行时空间（属主 pi）    │  │    ║
║  │  │  /dev/vdb → /mnt/data   │  │   │  │  /dev/vdb → /mnt/data   │  │    ║
║  │  │   ├ bind /workspace     │  │   │  │   ├ bind /workspace     │  │    ║
║  │  │   └ bind /home/pi/.pi   │  │   │  │   └ bind /home/pi/.pi   │  │    ║
║  │  │  Agent（uid 1001 非root）│  │   │  │  Agent（uid 1001 非root）│  │    ║
║  │  └────────────────────────┘  │   │  └────────────────────────┘  │    ║
║  └──────────────┬───────────────┘   └──────────────┬───────────────┘    ║
║                 │ tap-pi01                         │ tap-pi02           ║
╚═════════════════╪══════════════════════════════════╪══════════════════════╝
                  │                                  │
                  └──────────────┬───────────────────┘
                                 ▼
                        模型服务（公网 API）
```

---

## 二、目录结构（服务器上真实的样子）

```
/srv/fc/
├── base/
│   └── ubuntu-24.04-runtime.ext4     ★ 只读基础层（属主 root，801 MiB）
│                                       全平台共享一份，建议用模板盘
│
├── volumes/                          ★ 必须保护：用户数据
│   ├── tenant-A/
│   │   └── proj-alpha.img            稀疏文件 10G，实际占用按需
│   └── tenant-B/
│       └── proj-beta.img
│
└── images/                           内核镜像
    └── vmlinux.bin

/srv/jailer/firecracker/              ★ 可随时删除：实例状态
├── pi-01/
│   └── root/                        ← chroot 根（VMM 眼里的 /）
│       ├── rootfs.ext4  ────┐
│       ├── userdata.img ────┤ 硬链接（同一 inode）
│       ├── vmlinux          │
│       ├── firecracker.sock │
│       └── ...              │
└── pi-02/                   │
    └── root/                │
                             │
        ┌────────────────────┘
        │  硬链接指向母文件（零拷贝）
        ▼
   /srv/fc/volumes/tenant-A/proj-alpha.img

/var/lib/fc/                          实例元数据
├── instances/
│   ├── pi-01.json                    规格、IP、volume_path
│   └── pi-01.busy                    存在 = 有长任务，回收器跳过
└── activity/
    └── 10.0.1.2                      mtime = 最后活动时间
```

**关键约束:`/srv/fc/volumes/` 和 `/srv/jailer/` 必须在同一个文件系统上**（硬链接不能跨分区）。

```bash
df /srv/fc/volumes/ /srv/jailer/
# 两行的 Filesystem 必须相同
```

---

## 三、一条命令的启动流程

```
fc-up.sh --tenant tenant-A --workspace proj-alpha --id pi-01

  ①  查元数据
      工作区 → volume_path = /srv/fc/volumes/tenant-A/proj-alpha.img
      不存在？→ 建盘：truncate -s 10G → mkfs.ext4
                       → 挂载建 workspace/ pi-config/ → chown 1001

  ②  准备 chroot
      mkdir -p /srv/jailer/firecracker/pi-01/root

  ③  ★ 硬链接两块盘进 chroot（零拷贝，瞬时）
      ln /srv/fc/base/ubuntu-24.04-runtime.ext4 \
         /srv/jailer/.../pi-01/root/rootfs.ext4
      ln /srv/fc/volumes/tenant-A/proj-alpha.img \
         /srv/jailer/.../pi-01/root/userdata.img

  ④  建 tap 设备、分配 /30 地址（由 MAC 反推）

  ⑤  jailer 启动 VMM（加固 + 降权 + chroot）
      jailer --id pi-01 --exec-file firecracker \
             --uid firecracker --gid firecracker \
             --chroot-base-dir /srv/jailer \
             --cgroup-version 2 \
             -- --api-sock /firecracker.sock

  ⑥  通过 API 配置（★ 路径是 chroot 内的）
      PUT /boot-source   {kernel_image_path: "/vmlinux"}
      PUT /drives/rootfs   {path_on_host: "/rootfs.ext4",  is_read_only: true}
      PUT /drives/userdata {path_on_host: "/userdata.img", is_read_only: false}
      PUT /machine-config  {vcpu_count: 2, mem_size_mib: 1024}
      PUT /network-interfaces {host_dev_name: "tap-pi01"}

  ⑦  InstanceStart
      guest 内核启动 → 发现 /dev/vda、/dev/vdb
      init 脚本：
        mount /dev/vdb /mnt/data
        mount --bind /mnt/data/workspace  /workspace
        mount --bind /mnt/data/pi-config  /home/pi/.pi

  ⑧  注册状态
      /var/lib/fc/instances/pi-01.json
      /var/lib/fc/activity/<实例IP>     ← 回收器靠这个

  ✅  数秒后可用
```

---

## 四、三个信任域（安全设计的骨架）

```
┌─────────────────────────────────────────────────────────────┐
│  ① 隔离边界：microVM 虚拟化层                                 │
│     用户代码 ──✗──► 平台基础设施                              │
│     手段：KVM 硬件虚拟化 + 独立内核                            │
│     验证：实例内 uname -r ≠ 宿主 uname -r                     │
├─────────────────────────────────────────────────────────────┤
│  ② 凭据边界：Tool Gateway 入站侧                              │
│     不可信环境 ──► 可信凭据（单向）                            │
│     手段：实例内只有占位串，真凭据只在网关                     │
│     验证：实例内检索凭据为空，Agent 仍正常工作                  │
├─────────────────────────────────────────────────────────────┤
│  ③ 开发边界：本地开发机                                       │
│     ★ 生产环境【不存在】—— 服务器上直接跑 Firecracker          │
└─────────────────────────────────────────────────────────────┘
```

---

## 五、与原方案的对照：哪层消失了

```
本地（Mac + Lima）                    服务器（Linux 直装）
─────────────────────                ─────────────────────
L0  Mac 硬件                          L0  物理服务器
L1  ★ Lima 虚拟机  ← 消失             （不存在）
L2  Firecracker + jailer              L2  Firecracker + jailer
L3  microVM                           L3  microVM
L4  tap + NAT（在 Lima 内）            L4  tap + NAT（在宿主）
L5  Tool Gateway（在 Lima 内）         L5  Tool Gateway（在宿主）
L6  生命周期脚本                       L6  生命周期脚本
```

**链路从三层变两层**，启动更快、排查更简单，而且**这就是目标部署形态**。

---

## 六、搭建顺序（建议）

```
阶段 0  环境自检
        ├─ /dev/kvm 存在？          ls -l /dev/kvm
        ├─ CPU 支持虚拟化？          grep -E 'vmx|svm' /proc/cpuinfo
        ├─ 内核模块就绪？            lsmod | grep kvm
        └─ ❌ 任一不满足 → 换服务器，别往下走

阶段 1  ★ 脚本化构建基础镜像
        ├─ debootstrap 或官方 cloud image
        ├─ 装 Node 22 + Pi 运行时
        ├─ ★ 解压后显式 chown root:root（易漏，是已知缺陷）
        └─ 产出 base/ubuntu-24.04-runtime.ext4
        ⚠️ 这一步不做，后面全是重复劳动

阶段 2  单实例手动跑通
        ├─ 建一台 microVM（先不用 jailer，好排查）
        ├─ 验证：独立内核 / 网络连通 / 模型调用
        └─ 加上 jailer，跑八项安全验收

阶段 3  网络加固
        ├─ bridge + tap + iptables 规则
        ├─ ★ 加规则前先准备"救回 ssh"的手段
        └─ 验证：实例间不通、出网正常

阶段 4  治理与生命周期
        ├─ Tool Gateway（systemd 托管，别裸跑）
        ├─ 凭据注入 + 审计
        └─ 回收器 + 状态端口

阶段 5  一条命令端到端
        └─ fc-up.sh 一次跑通全链路，含数据保留验证
```

---

## 七、服务器选型硬要求（租之前必须确认）

| 项 | 要求 | 怎么确认 |
|---|---|---|
| **KVM** | 必须有 `/dev/kvm` | 问客服："支持 nested virtualization 吗" |
| 架构 | x86_64 最省事 | ARM64 也能跑，但生态和文档少 |
| 系统 | Ubuntu 22.04 / 24.04 | 内核 ≥ 5.10 |
| CPU | ≥ 4 核 | 每实例 2 vCPU，留 1–2 核给宿主 |
| 内存 | ≥ 16 GB 起 | **瓶颈是内存**，1 GB/实例 + 宿主开销 |
| 磁盘 | ≥ 100 GB SSD | 数据盘稀疏，实际占用看用户量 |
| 网络 | 固定公网 IP + 可加内网 IP | 网关和 NAT 需要 |
| 权限 | **root 或 sudo 完整权限** | 要建 tap、改 iptables、mknod |

**建议:先用按小时计费的独立服务器跑 2–3 天验证，别一上来包月。**

---

## 八、与现有材料的对应关系

| 本图 | 对应文档 |
|---|---|
| L2 jailer 动作 | `架构与实现方案汇报.md` §6.4 |
| L3 内部结构 | 同上 §3.1 |
| L4 网络规则 | 同上 §6.2 |
| 数据盘接入 | 同上 §5.4 |
| 硬链接原理 | 同上 §5.4.4 |
| 生命周期 | 同上 §6.6 |
| 搭建优先级 | 同上 §八（路线图阶段一～六） |
