# microVM 预热池实施计划

> 状态：设计与实施计划，尚未开发、部署或实测。
>
> 基线：2026-11-04 的当前仓库；Firecracker v1.17.0、Guest Linux 5.10.268、只读 rootfs、每用户一块持久化数据盘、SQLite 网络租约、Host Pi Gateway、按连接启动的 Pi RPC。
>
> 本计划先实现运行中的空白 Guest 预热池，并为未来完善后的用户快照恢复保留明确边界。文中的新文件、配置、状态及管理命令均为拟新增内容，不能当作当前可执行功能。

## 1. 目标与收益范围

预热的目标是把 Guest 内核启动和公共初始化从用户请求路径移到后台提前完成。用户需要计算资源时，平台领取一台已经启动、尚未接触用户数据的 microVM，再绑定该用户的数据盘和运行配置。

当前开发环境曾测得实例冷启动约 5 秒，其中进入 Guest init 前后的内核启动约 3 秒。这些是 Apple Silicon、UTM、Ubuntu ARM64 嵌套环境的历史样本，不能直接当成正式服务器的性能承诺。

第一阶段希望实现：池命中时无需再次加载内核和启动 Guest，业务仍通过现有 Control Plane、HTTP 和 SSE 使用实例。

### 1.1 必须分开的四个指标

| 指标 | 起止点 | 预热能够改善的范围 |
|---|---|---|
| 实例准备耗时 | 收到启动请求 → 用户卷绑定且 Guest RPC 就绪 | 主要优化对象 |
| Pi 就绪耗时 | 建立业务 RPC 连接 → Pi 能处理命令 | 当前仍涉及新 Pi 进程启动 |
| 历史显示耗时 | 请求会话历史 → 返回展示消息 | 主要由已实现的 SQLite 缓存改善 |
| 首个模型文本耗时 | 提交消息 → 首个模型文本事件 | 还包含 Pi 初始化、模型网络与供应商推理 |

因此，“预热接管完成”不能直接写成“Agent 在同样时间内给出回答”。本阶段不把 Pi 常驻改造并入预热，不以启动用户 Session 或调用真实模型作为补池条件。

### 1.2 建议的首期验收目标

在数据盘已存在、文件系统健康、系统资源充足的同一测试环境中，池命中的实例准备耗时 P95 相比同条件冷启动下降至少 40%；以 P95 不超过 1.5 秒作为初始工程目标。

上述数字是待验证目标。首次创建数据盘、文件很多导致递归权限处理变慢、磁盘检查修复、池未命中等情况单独统计，不通过排除失败样本来美化结果。

如果实测无法达到目标，应根据分阶段耗时继续处理绑定路径，而不是宣称已经得到亚秒级聊天体验。

## 2. 当前实现为什么不能直接形成预热池

### 2.1 用户初始化发生得太早

当前 `scripts/guest/agent-init.sh` 在 `agent_data_disk=1` 时立即执行：

```text
挂载 /dev/vdb
→ bind mount workspace 和 pi-state
→ 设置目录所有权
→ 检查工作区写入
→ 启动 Pi RPC 监听
```

这条路径默认启动时已经知道用户数据盘。匿名预热项不能直接复用这段完整流程，否则必须提前选择某个用户卷，失去通用分配能力。

### 2.2 业务实例与物理 VMM 身份耦合

当前 `InstanceRuntime.paths()` 用同一个实例 ID 生成 Jailer 路径、日志和活动文件；网络租约也以该 ID 登记；`lifecycle.py` 按这个 ID 校验和清理 jail。

预热 VMM 已有运行中的进程、Jailer 目录和网络。用户领取时不能简单改目录名，再把它当成另一台新启动的实例。

### 2.3 完整启动方法包含用户专属步骤

`InstanceRuntime.start()` 将用户卷准备、模型网关、网络、Jailer、Guest 启动和 watcher 登记串成一条流程。预热需要保留这些能力，但拆开调用时机：公共准备提前做，用户绑定在领取时做。

### 2.4 预热有真实资源成本

运行中的等待 Guest 仍占 VMM 进程、Guest 内存、TAP、租约和文件描述符。等待时可以低 CPU 占用，但不是零内存成本。

## 3. 推荐路线

### 3.1 第一阶段：运行中的空白 Guest 池

每台预热项提前完成：

1. 加载当前版本的只读 rootfs 和 Guest 内核。
2. 注册固定的 vCPU、内存、网卡及占位 data 设备。
3. 建立独立 `/30` 网络、TAP 和基础防火墙。
4. 完成 Guest 基础挂载与网络初始化。
5. 进入等待激活状态，开放专用激活入口。

此时不挂载用户卷，不启动业务 Pi，不加载 Session，不启动带真实模型凭据的 Host Gateway。

```text
后台：启动空白 Guest → WARM_READY → 等待分配
                                    ↓
请求：校验用户 → 原子领取 → 绑定用户盘 → 激活 → PI_RPC_READY
```

### 3.2 每台预热项只分配一次

预热项一旦被领取并开始用户绑定，不再返回公共 ready 池。绑定失败时销毁该项；绑定成功后按普通用户实例管理。

用户停止实例后，其 Guest 内存、页缓存和 Pi 状态可能含用户数据。首期不设计“清洗后再给其他用户”的复用流程，后台补充新的空白 Guest。

### 3.3 第二阶段：快照完善后的组合策略

用户自己的有效快照用于恢复该用户原实例；公共预热池用于没有可恢复运行状态的新一次启动。

公共基础快照如果未来落地，仅用于后台更快生成空白预热项。它不能来自某个真实用户实例，也不能包含其数据盘、Session、模型凭据或业务连接。

运行池可以先靠冷启动补充，并不要求先完成公共基础快照克隆。

## 4. 请求决策与快照的关系

### 4.1 未来统一的决策顺序

```text
用户请求计算资源
    ↓
已有可用运行实例？──是──→ 复用
    ↓ 否
已有该用户的暂停快照？──是──→ 进入专属快照恢复流程
    ↓ 否
存在兼容的 ready 预热项？──是──→ 领取并绑定
    ↓ 否
按原有冷启动流程创建
```

预热首先作为正常启动的内部策略，不改变 `agentctl chat` 的输入和 SSE 事件契约。当前 suspended 状态仍要求显式 resume；未来是否允许聊天自动恢复，应单独变更 API 语义。

### 4.2 快照失败不能静默借池启动

用户暂停快照仍与用户数据盘成对时，不允许另一台预热 Guest 同时挂载该盘并写入。否则之后恢复旧内存状态，可能与已经变化的磁盘不一致。

恢复失败时保留暂停状态和明确错误。若要改走预热或冷启动，必须先完成“放弃这份恢复状态”的显式操作：确认旧 VMM 已退出、撤销快照的恢复资格、确认卷可独占使用，再开始新启动。

不能用“resume 超时”作为立即释放用户卷的充分条件。

### 4.3 两类快照的保存边界

| 类型 | 归属 | 用途 | 用户数据盘关系 |
|---|---|---|---|
| 用户专属快照 | 一个用户、一个逻辑实例 | 恢复原工作状态 | 与原用户卷配对 |
| 公共基础快照，后续可选 | 某个运行规格版本 | 后台补充空白池 | 不绑定真实用户卷 |

两者应使用不同目录、manifest 类型和生命周期策略，不能只靠文件名区分。

## 5. Guest 的准备与激活

### 5.1 增加明确的预热启动模式

建议新增 `agent_prewarm=1`，在 `agent-init.sh` 中先识别该模式，必须早于现有用户数据盘挂载和 `chown -R` 分支。

将 Guest 初始化拆成可复用函数：

| 函数职责 | 冷启动 | 预热准备 | 领取后激活 |
|---|---|---|---|
| proc/sys/dev/run/tmp 初始化 | 执行 | 执行 | 不重复 |
| eth0、Host/Guest 地址配置 | 执行 | 执行 | 核对原配置 |
| 只读构建元数据检查 | 执行 | 执行 | 校验兼容版本 |
| 挂载用户 vdb | 执行 | 禁止 | 执行 |
| workspace/pi-state bind mount | 执行 | 禁止 | 执行 |
| 用户目录权限和写入探测 | 执行 | 禁止 | 执行 |
| 业务 socat / Pi RPC 监听 | 执行 | 禁止 | 执行 |

两个模式共享函数，避免复制一整套越来越不一致的 init。正式冷启动回归测试必须继续通过。

### 5.2 占位块设备

预热配置保留 `drive_id=data`，将其注册为第二块设备 `/dev/vdb`，但后端先指向预热项专属的无用户内容占位文件：

```text
rootfs → /rootfs.ext4 → Guest /dev/vda，只读
data   → /placeholder.ext4 → Guest /dev/vdb，尚未挂载
```

首期占位后端使用与当前用户盘相同的 1 GiB 逻辑容量、可写设备属性，不需要创建可用的 ext4 文件系统。文件名后缀不代表其中一定已经格式化。

该设备不得被 mount、fsck、自动探测脚本或 Pi 读取为业务盘；不得在等待阶段形成用户文件系统的挂载或脏缓存。初期用相同容量降低设备容量变更的复杂性，扩容另行设计。

Firecracker v1.17.0 提供启动后的 `PATCH /drives/{drive_id}`，更新已存在块设备的 `path_on_host`。这为延迟替换后端提供 API 基础，但是否能安全挂载仍取决于本项目的 Guest 协调流程。[官方 API 定义](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/firecracker/swagger/firecracker.yaml)、[块设备更新结构](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/vmm_config/drive.rs)。

### 5.3 明确的激活信号

仅 PATCH 后端不能让当前 init 自动重跑用户挂载逻辑。需要一个仅在预热等待期运行的 Guest 激活入口。

首期建议复用 TCP、socat 和 Bash，在 Guest 的独立端口 `8081` 实现一次性激活，不启动 Node 来解析激活请求，以免把 Pi 的进程启动成本引入接管路径。

建议协议采用固定字段单行记录，而不是接受任意命令：

```text
ACTIVATE_V1 <claim_token> <activation_id> <expected_volume_uuid>
```

字段采用固定字符集合和长度，限制单行大小与读取超时。处理程序只允许执行预先写好的挂载、校验和启动函数，不接受 shell 表达式、任意磁盘路径、任意程序或用户 Prompt，不使用 eval。

`claim_token` 是 Host 为本预热项生成的一次性控制凭据；`activation_id` 是每次领取生成的操作代次标识；UUID 只用于确认块设备匹配，不能代替 Host 数据库的所有权校验。

激活入口应由 root 初始化环境持有，因为挂载需要 Guest root 权限。它不是 Agent 管理器，不负责任务队列、Session 切换或模型调用；业务 Pi 仍降权为 pi 用户运行。

### 5.4 激活端口的访问边界

仅绑定该 Guest 的 TAP 私网地址，限定本实例 Host IP 来源，阻止其他 Guest 的转发访问，不转发到客户端或公网。控制入口的本地凭据文件只允许 Guest root 读取。

当前 Host 网络驱动只处理部分 Host input/forward 策略，新增控制入口还需要 Guest 侧绑定和来源限制；不能把“私有 IP”本身等同于已经完成鉴权。

激活凭据不写到普通日志、业务数据库事件或用户数据盘。建议通过预热启动的临时受限配置传入；如果使用内核参数，应明确 Host/Guest root 可以读取，并在用户接管前关闭入口、删除可删除的临时凭据。未来公共快照不能复制已生效的激活凭据。

如果最终使用 vsock 替代 TCP 激活，应另做设备和内核支持探针。Firecracker 的 vsock 可以通过 Host Unix socket 连接 Guest 监听端口，但当前仓库没有配置它，不能作为现成依赖。[官方 vsock 说明](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/vsock.md)。

### 5.5 接管后的 Guest 操作

Host 确认 PATCH 成功并发送激活记录后，Guest 执行：

1. 校验一次性凭据及代次，拒绝重复或不合法请求。
2. 确认 `/dev/vdb` 没有挂载，并按受控步骤清理未挂载设备的旧缓存；此步骤应在技术探针中验证。
3. 读取预期容量和 ext4 UUID，与 Host 提供的信息比对；失败时终止激活。
4. 将 `/dev/vdb` 挂载到 `/mnt/agent-data`。
5. bind mount workspace 和 pi-state。
6. 检查目录权限，并执行与本次挂载有关的写入探测。
7. 启动现有非 root 的 Pi RPC 监听循环。
8. 返回带 activation_id 的 `BOUND_READY`，写入串口诊断记录，进入只允许查询和确认的状态。
9. Host 核对结果后发送 `COMMIT_V1 <claim_token> <activation_id>`；Guest 写入确认标志并关闭 root 激活监听。
10. Host 确认激活入口已退出后，才允许该实例开始承接业务 RPC。

Host 同时验证预期端点已监听和代次匹配。`BOUND_READY` 只是 Guest 完成绑定的证据之一，最终实例归属仍以 Host 的绑定事务为准。

未领取时，巡检使用带控制凭据的 `PROBE_V1`，返回空白等待状态、runtime_id 和协议版本；不通过业务 `get_state` 启动 Pi 来证明预热健康。

ACTIVATE 成功后不允许再次执行挂载。COMMIT 之前，入口仅接受同一凭据和 activation_id 的 `STATUS_V1`、`COMMIT_V1`；Host 可查询状态，确认消息丢失时也能检查带该代次的串口标志。COMMIT 后入口关闭，后续恢复核对依据 Host 登记、PID 身份和本次串口记录，不假设还能访问已关闭的状态接口。

初始化状态必须保存在 Guest root 持有的 `/run` 中，当前 socat 子进程退出再连接不能把已完成激活重置成未激活。该状态机只在本次接管期间存在，异常超时由 Host 停止整个预热项。

## 6. 业务实例与物理运行实例的映射

### 6.1 两种 ID

| 标识 | 示例 | 稳定范围 |
|---|---|---|
| logical_instance_id | cli-demo | 用户看到的业务实例 |
| runtime_id | warm-<随机ID> | 一次物理 Guest 资源身份 |
| activation_id | 随机操作 ID | 一次绑定或恢复尝试 |

预热项领取后，物理 runtime_id、Jailer 目录、PID、slot、IP、MAC 和 TAP 保持原样。新增的只是“用户业务实例指向这台物理 Guest”的关系。

不重命名正在运行的 jail，不重新 allocate 一套网络，不把 warm ID 暴露成用户的新实例名。

### 6.2 建议保留当前表名，明确内部语义

当前 `runtime_instances.instance_id`、`runtime_sidecars.instance_id` 和 `network_leases.instance_id` 可以暂时保留列名，但在新实现中统一保存物理 runtime_id。

业务 `instances.id` 继续保存逻辑实例 ID。通过新的绑定表解析到物理运行记录。必须在存储接口中封装这层转换，避免业务代码任意跨表猜测。

旧实例迁移时，物理 ID 可先等于原逻辑 ID，维持已有路径；新预热项使用独立 warm ID。该约定要覆盖冷启动、查询、heartbeat、busy、stop、destroy、快照和恢复全部路径。

### 6.3 不可只修改 start()

以下调用必须同步适配：

- `InstanceRuntime.paths()`：物理资源路径按 runtime_id；用户盘按 user_id。
- `InstanceRuntime.status()`：由逻辑 ID 解析运行记录，再返回业务需要的端点。
- Pi Gateway 路由：仍从逻辑实例获取 Guest IP，不关心预热来源。
- lifecycle register/watch/stop：操作物理 ID，并携带本次运行代次。
- cleanup_ephemeral：根据经过校验的 runtime_id 确认 jail 根目录，不能放宽为任意数据库路径。
- 日志、活动文件及 busy：以物理运行记录定位。
- 快照 manifest：同时保存逻辑 ID、物理 ID、属主、版本和配对卷。

当前代码依赖 ID 的地方必须先做清单，完成兼容回归后再接入预热，避免只启动成功却停止时清理另一套资源。

## 7. SQLite 状态与并发领取

### 7.1 拟新增 prewarm_items

| 字段组 | 建议内容 |
|---|---|
| 主键 | runtime_id |
| 版本规格 | profile_id、rootfs_build_id、pool_generation |
| Host 身份 | host_boot_id、PID、process_start_ticks |
| 网络 | 对应 runtime_id 的租约引用 |
| 状态 | preparing、ready、claiming、assigned、draining、error、retired |
| 领取信息 | logical_instance_id、user_id、activation_id、claim_deadline |
| 时间和诊断 | created_at、ready_at、expires_at、last_error |

不在该表中保存真实模型 Key 或明文激活凭据。需要重启后继续激活时，凭据应从权限受限的 Host 临时文件读取，或将未完成项销毁。

### 7.2 拟新增 runtime_bindings

保存 logical_instance_id、user_id、volume_path、runtime_id、activation_id、状态和更新时间。

关键约束：

1. 一个逻辑实例至多绑定一个当前物理运行实例。
2. 一个 runtime_id 至多分配给一个逻辑实例。
3. 一个用户的数据盘同时至多有一个可写运行占用。
4. binding、running、releasing、suspended 和尚未清理的 error 均不得被当成空闲卷。

正常 stop 完成后释放物理占用；suspended 保留快照配对和用户盘的使用资格，直到恢复或明确放弃。表可以保留逻辑记录，将已停止的 runtime_id 清空；稳定 user_volumes 绑定继续保留。

### 7.3 原子领取事务

在一个短 `BEGIN IMMEDIATE` 事务中：

```text
检查用户和业务实例状态
→ 确认没有活动绑定或暂停快照冲突
→ 选择匹配profile且未过期的ready项
→ 条件更新 ready → claiming，检查受影响行数
→ 预留用户卷独占占用
→ 写入runtime_bindings与activation_id
→ 将业务实例置为starting
→ 提交
```

查询和条件更新、唯一约束共同保证两个请求不能领到同一项，也不能把同一用户卷交给两个 VMM。

e2fsck、mount、启动进程、等待网络、PATCH 和 Guest 激活都在事务外执行，通过状态和补偿流程推进，不能长时间占用 SQLite 写锁。

### 7.4 超时与 fencing

claim_deadline 用于识别可能卡住的操作，不代表一到时间就允许重新分配。旧进程可能仍在执行 PATCH，旧 Guest 也可能仍持有用户盘。

每次后续操作都核对 runtime_id 与 activation_id，防止过期回调把新一轮状态覆盖。涉及 VMM 和磁盘的操作还需使用本机资源操作锁或等价串行机制，清理前确认旧执行者不会继续修改资源。

数据库唯一约束无法自动切断已经打开的磁盘文件描述符。不确定时标记 error/隔离，停止并确认旧 VMM 退出，才释放用户卷。清理未完成前不启动另一台 Guest 使用该盘。

## 8. 网络租约与凭据

### 8.1 预热时就占用网络

预热项通过现有 NetworkManager 申请独立 slot，以物理 runtime_id 登记。Guest 网络启动时已确定 IP，领取后沿用该网络，省去重新配置 Guest 网络。

领取只更新业务绑定，不转移或释放该 slot。所有 ready 预热项都计入地址池容量与 Host 网络资源使用量。

建议现有 network_leases 的 reserved/active/releasing/error 继续表示网络资源状态；空白 Guest 网络检查通过后可为 active。是否属于公共池、是否已绑定用户由 prewarm_items 表示，不新增混乱的网络状态。

### 8.2 等待期的策略

等待期允许 Host 激活控制和必要的连接响应，不启动真实模型网关，不允许 Guest 直接出网或访问其他 Guest。

当前 `_build_firewall_rules()` 要求非空 allowed_host_ports，因此不能直接传空元组冒充“无业务端口”。实现时新增明确的预热策略分支，仅放行 Host 主动连接的响应；不能为预热放宽所有端口校验。

绑定阶段安装正常模型访问规则并启动本实例 Tool Gateway，待 Guest 初始化成功后完成策略切换。

策略切换需要新增只更新本实例 nftables 表的方法，例如 `LinuxNetworkDriver.update_policy()`，以原子规则批次应用；不能再次调用会创建 TAP 的 setup/create，也不能清空 Host 全局防火墙。切换失败时保持受限状态并终止接管。

### 8.3 用户卷与模型配置

仍使用 `UserVolumeStore` 确认用户卷路径。Host 在确定独占后检查文件系统，临时挂载写入该预热项 Host IP 对应的 models.json，sync 并卸载，再提供给 Firecracker。

真实 Key 仍只进入 Host 的受限网关凭据副本；不用第三块配置盘。当前每用户一块盘的规则不因预热改变。

## 9. 后台补池与资源预算

### 9.1 首期固定小池

| 配置项，拟新增 | 建议初值 | 含义 |
|---|---|---|
| PREWARM_ENABLED | 0 | 默认关闭，显式开启 |
| PREWARM_TARGET_READY | 1 | 开发环境保留一台可领取项 |
| PREWARM_MAX_TOTAL | 2 | 公共池 preparing/ready/claiming/draining/error 等未清理项上限 |
| PREWARM_BUILD_CONCURRENCY | 1 | 限制后台启动并发 |
| PREWARM_TTL_SECONDS | 900 | 未领取项最长等待时间 |
| PREWARM_HEALTH_INTERVAL_SECONDS | 5 | 健康巡检间隔 |
| PREWARM_CLAIM_TIMEOUT_SECONDS | 30 | 异常领取审计阈值 |
| PREWARM_ACTIVATE_TIMEOUT_SECONDS | 10 | Guest 激活的等待上限 |
| PREWARM_MIN_AVAILABLE_MIB | 2048 | 补池后建议保留的 Host 可用内存预算 |

以上是初值，不是容量承诺。TTL、巡检和启动超时分别管理不同阶段，不能混为一个“300 秒回收时间”。

### 9.2 规格匹配

profile 至少包含架构、Firecracker 版本、Guest 内核、rootfs Build ID、2 vCPU/1024 MiB、UID/GID、网络策略版本与激活协议版本。

用户请求与 profile 不匹配时走正常冷启动，不能把已经启动的 1 GiB Guest 当成任意规格使用。rootfs 更新后，新请求不再领取旧版本项；后台逐步 drain 旧项并补充新版本。

### 9.3 内存与启动压力

当前每项配置 1 GiB Guest RAM，还需要 VMM 和 Host 额外空间。实际 RSS 会变化，容量规划应使用实测峰值与保守预算，而不是假设所有页已物理分配或所有空闲页免费。

若等待一台使用约 M 的实测内存预算，保留 N 台约需要 N×M，再加 Host、活动实例和启动峰值余量。最初测量 N=0、1、2，确认收益后再扩容。

用户真实请求优先于补池。Host 内存低、正在大量冷启动或正在构建 rootfs 时暂停补池；失败采用有上限退避，避免无限重建。预热池只搬移启动成本，不增加服务器实际可承载的用户总量。

### 9.4 池维护进程

建议新增一个 Host Python worker，单机只有一个维护者；通过现有服务脚本管理启动和停止，用本机锁或明确的数据库领导权防止重复补池。

worker 负责补池、健康检查、过期清理和启动后核对。Control Plane 请求线程只负责领取与绑定。二者共享短事务和同一资源编排方法。

公共 ready 项由池 TTL 管理，不登记普通用户的 300 秒 idle watcher。接管时先持久化物理运行元数据，再提交 assigned/running 与业务绑定，最后启动并记录用户 watcher 的 PID/启动 ticks，确认成功后返回请求。Control Plane 的恢复核对负责补启缺失 watcher，且需校验代次，避免重复派生。

提交后若 watcher 启动失败，按已绑定用户实例的故障处理，不能退回 ready。池 worker 只处理未分配项，避免两个回收器同时清理同一资源。

## 10. 一次完整的预热接管

### 10.1 后台预热

1. 读取配置，确认空白池缺额与 Host 预算。
2. 创建唯一 runtime_id，写 preparing 记录及 Host boot ID。
3. 预留网络，创建 TAP 和等待期防火墙。
4. 创建独立占位后端，准备只读系统盘和内核硬链接。
5. 配置 2 vCPU、1024 MiB、data 占位设备及预热启动模式。
6. 通过 Jailer 启动 Guest。
7. 等待带本项身份的 WARM_READY，并验证激活端点、VMM PID/start ticks、未挂载用户盘状态。
8. 保存运行元数据与 TTL，将 preparing 改为 ready。

仅检测进程存在不算 ready，历史日志中的旧标志也不能当成本次启动证据。

### 10.2 用户领取

1. 校验平台登录、实例归属和用户配额。
2. 复用已有运行实例；有暂停快照时进入专属恢复路径。
3. 在 SQLite 原子领取兼容 ready 项，并建立用户卷独占绑定。
4. 复核 VMM 身份、版本、激活代次、网络和占位状态。
5. 创建或查找用户卷，执行 e2fsck；离线更新 models.json 后卸载。
6. 启动 Host Tool Gateway，检查其监听就绪。
7. 将用户卷硬链接到该 jail 的 `/data.ext4`，确认 Host 没有仍挂载该卷。
8. 调用 `PATCH /drives/data` 将后端从 `/placeholder.ext4` 切到 `/data.ext4`。
9. Host 生成并发送本次 ACTIVATE_V1，Guest 挂载并启动 Pi RPC 监听。
10. 核对 BOUND_READY 的 activation_id 和预期端点。
11. 完成激活 COMMIT，确认控制入口退出；持久化物理运行记录、sidecar 和 activity，提交 assigned/running 与绑定，最后启动并确认用户 watcher。
12. 返回原有实例响应，后台异步补池。

用户请求不应等待“补回另一台预热 Guest”才返回。

### 10.3 后端替换的 API 示例

以下是未来技术探针中使用的操作形式，不是现在对正在运行的用户实例直接执行的指令。前提是目标仍是未挂载占位盘的空白 Guest，用户盘已经独占、离线准备并进入该 jail。

```bash
sudo curl --fail-with-body --silent --show-error \
  --unix-socket /srv/jailer/firecracker/warm-example/root/firecracker.socket \
  -X PATCH http://localhost/drives/data \
  -H 'Content-Type: application/json' \
  -d '{"drive_id":"data","path_on_host":"/data.ext4"}'
```

`/data.ext4` 是该 Firecracker chroot 内的路径，不是 Host 根目录下的文件。仅改变硬链接名称或替换 Host 路径，并不会自动让已经打开的块设备切换后端，必须走对应设备更新流程。

### 10.4 池未命中

无兼容 ready 项时，立即记录 miss 并走现有冷启动，不无限等待后台补池。可设置严格且很短的领取等待预算；首期优先使用不等待策略。

如果已经进入用户盘绑定后失败，只有确认该物理 Guest 退出、磁盘不再被使用、绑定可安全释放后，才允许冷启动兜底。清理超时则返回明确错误，而不是重叠启动。

## 11. 异常补偿与重启恢复

| 发生位置 | 处理 |
|---|---|
| preparing 启动失败 | 清理 VMM、TAP、占位文件和 jail；失败残留进入 error |
| ready 过期或版本不兼容 | 条件更新为 draining，清理后 retired |
| ready 的进程已退出 | 移出可领取集合，清理网络并补池 |
| claiming 时用户盘检查失败 | 终止本次领取并销毁该预热项；保留用户数据 |
| PATCH 失败 | 视为该项不可继续分配，停止并核实资源状态 |
| 激活响应丢失 | 按同一 activation_id 查询结果，不能直接重复 mount |
| Guest 已绑定、Host 未提交 running | 根据代次和真实资源核对；不能再领一台使用同一盘 |
| Control Plane 重启 | 从 SQLite 恢复绑定并校验 PID、进程 ticks、Host boot ID |
| 整台 Host 重启 | 旧进程视为失效，但先核对挂载/资源与卷占用再清理记录 |
| 网络清理失败 | 保留 error 租约，不把 slot 重新分配 |

清理操作应幂等。每个资源有明确所有者和代次，不能仅按名字前缀删除所有 `warm-*`，也不能在状态不确定时删除用户卷。

worker 对 ready 的过期回收与请求线程的领取使用同样的条件更新：只有 ready 可以进入 claiming 或 draining，成功者取得操作资格。

## 12. 与未来快照完善后的协同

### 12.1 用户快照路径

完善后的 resume 应继续使用用户专属磁盘、镜像版本与运行绑定；重新创建 Host 进程和网络后，恢复该用户 Guest 状态。

预热上线不要求把现有用户快照转换为公共池，也不允许用公共 Guest 覆盖仍有恢复资格的用户卷。要兼容本计划的新 runtime_id 映射，应明确 manifest 版本并做迁移，不能猜测旧文件含义。

### 12.2 公共基础快照，作为后续补池优化

完成运行池后，才评估在“无用户数据、未挂载 vdb、尚未开放业务 RPC”的公共阶段生成模板。后台从模板恢复空白项，重新建立每项身份与网络，再进入相同 WARM_READY。

Firecracker 的快照加载可以覆盖 Host TAP 名称，但这不等于自动改变 Guest 内部已保存的 IP/MAC。公共模板若带已初始化网络，克隆可能保留重复 Guest 网络参数。必须另有受控 bootstrap 通道完成重配，或者在网络个性化之前取模板。[官方网络克隆说明](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/network-for-clones.md)。

这时可评估 vsock 或 MMDS 作为模板个性化入口；不能依靠写一个新 boot_args，就假设恢复出的内核会重新执行启动流程。

### 12.3 模板克隆的独特风险

重复恢复同一模板可能复制随机数状态、标识和用户态令牌。当前 Guest 5.10.268 不能被假定具备新内核的 VMGenID 随机状态更新能力。模板方案必须先验证 Guest 内核、平台和安全随机数恢复机制，并在每个克隆中重新生成身份与控制凭据。[官方快照安全说明](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/snapshot-support.md)。

此外，模板需要按 CPU/架构、Host 内核、Firecracker、Guest 内核和 rootfs 版本管理兼容性。[官方快照兼容性说明](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/versioning.md)。

这些门槛未通过前，后台继续冷启动补池。不能把公共快照克隆当成运行池第一阶段已经具备的能力。

### 12.4 快照不释放运行池的成本

ready 项如果保持运行，仍占内存；如果将所有 ready 项保存到磁盘并退出，它们就成为待恢复模板或暂停项，请求路径仍需执行恢复。

可以后续组合少量内存中的 ready 项和磁盘模板，但应分别统计命中率、恢复成本与资源占用，不能统一宣传为同一种“热启动”。

## 13. 模块和文件实施清单

所有新增路径均为建议，实际开发时保持现有命名习惯即可，不引入 Redis、消息队列或 Kubernetes。

| 文件 | 拟改动 |
|---|---|
| `services/instance-manager/runtime.py` | 抽取公共准备/用户绑定；保留冷启动；按 runtime_id 定位资源 |
| `services/instance-manager/runtime_store.py` | 运行绑定、代次和历史兼容迁移 |
| `services/instance-manager/network_manager.py` | 预热网络策略、沿用物理租约、保持清理幂等 |
| `services/instance-manager/lifecycle.py` | 物理 ID 路径校验、用户 watcher 接管、代次校验 |
| `services/instance-manager/prewarm_store.py`，新 | SQLite 预热状态和原子领取 |
| `services/instance-manager/prewarm_manager.py`，新 | prepare/claim/bind/drain/reconcile 编排 |
| `services/instance-manager/prewarm_worker.py`，新 | 单 Host 补池、巡检和 TTL |
| `scripts/guest/agent-init.sh` | 公共初始化与用户激活分离 |
| `scripts/guest/prewarm-activate.sh`，新 | 固定协议的一次性激活与状态确认 |
| `scripts/linux/build-agent-rootfs.sh` | 安装新 Guest 文件，将其纳入 Build ID |
| `scripts/linux/control-plane-service.sh` | 管理补池 worker 生命周期 |
| `services/control-plane/server.py` | 内部启动策略和绑定解析，不改变业务消息协议 |
| `config/prewarm.env`，新 | 开关、数量、超时和资源预算 |
| `scripts/linux/prewarm-smoke-test.sh`，新 | 真实预热接管与清理验收 |
| `run.sh / run-linux.sh` | 对应配置、诊断和 smoke test 入口 |

`services/agent-runtime/agent_runtime.py`、PiGateway 的 execute/get_messages 和现有 Guest Pi Session 脚本原则上继续复用；只有端点解析和启动时机受影响。Pi Session 存储位置不因预热移动。

## 14. 分阶段开发与完成条件

### 阶段 0：固化基线与隔离实验

先整理当前文档改动，在新的 `codex/prewarm-pool` 分支开发。记录运行中的实例、数据盘与快照；实施 Guest 镜像改造前停止测试实例，保留用户盘。

运行现有网络、CLI、恢复测试，采集同条件冷启动、Pi 就绪和历史缓存命中耗时。用专用测试账户和测试卷验证预热，不使用业务用户数据做第一次磁盘替换实验。

完成条件：有可重复基线、可回滚的代码点和独立测试资产。

### 阶段 1：验证“等待 → 后端替换 → 激活”

先做一台专用探针，不建自动池：增加预热 Guest 分支、占位盘、激活入口和三个就绪标志；从 Host 调用设备 PATCH；确认 Guest 挂到测试 ext4 并开放 Pi RPC。

检查占位盘始终未挂载，测试卷 UUID 正确，Host 已卸载，写入标记停止后可读取。分别模拟错误 UUID、错误 token、重复激活和响应丢失。

完成条件：连续至少 30 次替换成功，错误输入不会执行任意命令、重复挂载或触碰其他卷。未通过前不开发池自动补充。

### 阶段 2：拆分运行身份与兼容旧实例

引入逻辑 ID → runtime_id 映射，迁移旧运行记录；让冷启动先完整经过新映射，暂时关闭预热领取。

逐项验收 start/status/chat/heartbeat/busy/stop/destroy、空闲回收、控制面重启、VMM 崩溃清理及已有快照路径。

完成条件：预热关闭时，已有业务行为与数据保持一致，生命周期清理使用正确物理资源。

### 阶段 3：实现 SQLite 原子领取和卷独占

实现新表、条件状态更新、activation_id、领取超时和恢复核对。先用模拟 VMM 测并发与补偿，再连接阶段 1 的真实探针。

重点测试两个用户抢一台项、同用户同时两次 start、领取时 worker 正在回收、进程在事务提交后退出等情况。

完成条件：无重复领取、无同卷双写、无超时后旧操作覆盖新状态。

### 阶段 4：接入 Control Plane 和正常生命周期

加入复用/暂停快照/预热/冷启动的内部决策，绑定完成后登记原有用户 watcher。池未命中直接冷启动，接管失败按可核实的资源状态决定是否兜底。

完成条件：CLI 无需学习新聊天命令，同一业务实例名可稳定使用两种启动路径，停止后资源完整清理。

### 阶段 5：补池、资源控制与回滚

增加单 worker、target=1、TTL、退避、内存门槛、版本 drain、服务重启 reconcile 和管理诊断。开启后进行长时间空闲和连续领取测试。

完成条件：池不会无限增长，不干扰用户任务，不会在服务重启时留下多个补池进程。

### 阶段 6：性能验收与后续快照协同

完成冷热对照、多用户并发和故障测试；达到目标后记录实测分布。用户快照完善后再复核专属恢复优先级；公共模板克隆另设兼容和随机状态门槛。

完成条件：有明确优化幅度、已知限制、关闭步骤和回归证据。

## 15. 操作与验收步骤

### 15.1 当前可执行的准备检查

在 macOS 项目目录查看仓库和当前实例：

```bash
git status --short
./agentctl status
```

在 Ubuntu Host 检查：

```bash
ls -l /dev/kvm
free -m
df -h /srv /var/tmp
command -v firecracker
command -v jailer
sudo cat /srv/fc/artifacts/agent-rootfs.build-id
```

源码构建与网络准备不在用户等待时执行。准备镜像的流程继续由 `run.sh start` 或 `run-linux.sh start` 提前完成。

### 15.2 拟新增管理命令

下列命令仅表示后续实现的操作界面，目前不能执行：

```text
./run.sh prewarm-status
./run.sh prewarm-fill
./run.sh prewarm-drain
./run.sh prewarm-smoke-test
```

Ubuntu 直接部署提供 `run-linux.sh` 对应入口。管理输出至少包含版本、preparing/ready/claiming 数量、runtime_id、PID、Guest IP、年龄、到期时间与错误；不输出凭据。

fill 尊重资源和数量上限；drain 停止接纳新领取并清理未分配项；assigned 的用户实例不因公共池 drain 被关闭。

### 15.3 单用户对照

1. 关闭预热，停止同一测试实例。
2. 发起正常 start 或 chat，记录冷启动到 RPC READY、Pi 就绪、首个文本事件。
3. 正常停止并保留同一测试盘。
4. 开启预热，等待 status 确认 ready=1。
5. 再发相同请求，确认 startupMode=prewarm 且沿用原池项的 PID/slot。
6. 检查用户文件和会话连续性。
7. 停止用户实例，确认该物理项退出，后台生成新的 runtime_id 补位。

对照时工作区、数据规模、模型、Host 负载和缓存条件保持一致；不得将第二次自然缓存升温的全部收益归给预热。

### 15.4 多用户与故障验收矩阵

| 场景 | 必须观察到的结果 |
|---|---|
| 两个用户争抢一台 ready | 仅一人命中，另一人冷启动或按明确策略等待 |
| 同用户同时启动两次 | 至多一个有效运行绑定 |
| 同卷已被运行 Guest 使用 | 新接管被拒绝，不执行 Host 可写挂载 |
| 用户有 suspended 快照 | 不领取公共池使用其配对卷 |
| 无 ready 项 | 正常冷启动，业务可用 |
| 卷检查失败 | 保留用户盘，销毁已领取项，返回可定位错误 |
| 绑定前后杀 Control Plane | 不重复分配，重启后可核对并清理 |
| 杀 pool worker | 不影响已绑定用户任务，重启无重复维护者 |
| 领取后 Guest 无响应 | 不将该项放回 ready，不启动同盘第二个 VMM |
| 网络清理失败 | 保留 error 租约，slot 不复用 |
| rootfs/profile 更新 | 旧 ready 不再被领取，按版本补池 |
| Host 内存不足 | 停止补池，用户请求优先 |
| 恶意激活输入 | 超限、错 token、任意路径或 shell 文本均被拒绝 |

## 16. 计时与统计

建议在现有 startupTiming 外增加可选诊断，不改变消息事件的业务含义：

```text
startupMode = cold | prewarm | snapshot
poolLookupMs
poolClaimMs
volumePrepareMs
gatewayReadyMs
drivePatchMs
guestActivateMs
lifecycleRegisterMs
requestToReadyMs
poolMissReason
```

后台另记 poolPrepareMs、readyWaitAge、补池失败率、TTL 回收、RSS/内存压力。后台 poolPrepareMs 不算进已命中用户的 requestToReadyMs，但必须记录为系统成本。

每种路径至少采集 30 个有效样本，报告 P50、P95、最大值和失败数。首轮性能之外，再做至少 100 次领取/停止循环检查资源泄漏。正式容量判断需要更多持续负载样本。

成功口径同时满足：耗时下降、没有重复分配、用户数据一致、资源有上限、故障可恢复。只测一次最快值不能判定完成。

## 17. 发布与回滚

1. 默认关闭，先启用单 Host、单规格、专用测试用户。
2. 预热 target 从 1 开始，验证后再调整。
3. 发布新 rootfs/profile 时，先阻止旧版本领取，再 drain 和补池。
4. 发现问题先关闭新领取，保留现有用户实例正常运行。
5. 清理未绑定项；有不确定磁盘占用的 claiming/error 项先隔离核对。
6. 关闭 worker 后，确认没有未清理 VMM、TAP、nftables 和租约。
7. 冷启动路径始终保留，使用相同运行映射正常工作。

数据库迁移采用新增表和可回填字段，不在第一版删除旧列。功能关闭不等于可以直接回滚到不认识 runtime_id 的旧程序；代码回滚前需先停止并清理新格式的运行资源，或确保旧版读取兼容。

## 18. 推荐的第一项实际开发任务

首先开发阶段 1 的单项技术探针：让一个没有用户数据的 Guest 在等待点停留，证明已注册 data 设备可以切换到测试卷，并经一次性激活完成正确挂载与 Pi RPC READY。

它直接验证本方案最重要的底层前提。该探针通过后，再做物理运行映射、数据库领取和自动补池；用户快照恢复和 Pi 常驻优化继续保持各自的实施与验收边界。
