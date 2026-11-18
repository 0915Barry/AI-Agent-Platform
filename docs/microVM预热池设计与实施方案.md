# microVM 预热池设计与实施方案

> 文档状态：阶段 0～6.3 已完成；预热 STOP 竞态已修复并通过真实环境 30+30 验收。预热功能仍默认关闭，尚未进入上线后的生产灰度。
>
> 当前进度日期：2026-11-18。
>
> 当前结论：数据库状态机、数据盘独占、真实 Runtime 接管、Control Plane 启停清理和自动补池 Worker 已经落地。阶段 5 已通过 100 轮「领取/停止/补位」真实 VM 压力验收。阶段 6.1 已完成冷/热统一 `startupOperationTiming`，并补齐 `guestDeclaredRpcReadyMs` 真实跨层传递。阶段 6.2 正式 30+30 结果为冷启动 P95 `6188 ms`、预热启动 P95 `1195 ms`，平台侧启动耗时下降约 `80.7%`。阶段 6.3 已实现自动判定与资源成本采样：新版 3+3 验收降幅 `83.74%`，成功率 100%、回退率 0，单台空闲预热 VMM PSS 约 `65 MiB`并占用 1 个 `/30` 网络槽位。详细结论见 `docs/预热池阶段6性能验收报告.md`。

## 1. 目标与边界

预热池用于把与用户无关的 microVM 启动成本提前放到后台完成。后台先启动若干空白 Guest，完成内核、只读 rootfs 和网络初始化；用户请求计算资源时，平台领取一台已经达到 `ready` 的空白 Guest，绑定用户数据盘并启动 Pi RPC。

```text
后台补池：空白 microVM → WARM_READY → 等待领取
                                      ↓
用户启动：逻辑实例 → 原子领取 → 绑定数据盘 → 激活 Pi RPC → running
```

预热池首期只优化“实例准备耗时”，不等同于以下指标：

- Pi 原生 Session 加载耗时；
- 历史消息展示耗时；
- 模型首次响应耗时；
- 首次创建或修复用户数据盘的耗时。

预热池不是快照的替代品。快照恢复某个用户原来的运行现场，预热池提供尚未属于任何用户的公共空白 Guest。

## 2. 可行性结论

### 2.1 已经成立的底层前提

当前仓库已经验证：

1. Guest 可以通过 `agent_prewarm=1` 进入空白等待模式；
2. 等待期间不挂载用户数据盘、不启动业务 Pi RPC；
3. Firecracker 可以通过 `PATCH /drives/data` 把占位盘后端替换为真实 ext4 用户盘；
4. Guest 激活程序能够校验 Token 和数据盘 UUID，完成 mount、bind mount 和权限检查；
5. 激活完成后可以启动 Pi RPC，并通过真实 RPC 请求验证可用性；
6. 错误 Token、错误 UUID 和重复激活会被拒绝。

因此，“提前启动公共 Guest，用户到来后再绑定数据盘”的技术路线可行。

### 2.2 当前实现边界

当前已经实现：

- `prewarm_items`、`startup_operations`、`volume_attachments` 及代次条件转换；
- `PrewarmManager.claim()/commit()/complete_stop()` 与逻辑/物理 Runtime 绑定；
- 正式 `NetworkManager`、`RuntimeStore` 和 `InstanceRuntime` 上的空白 Guest 创建、热切盘、激活和提交；
- 快照暂停保留网络槽位，恢复时复用原租约；
- `StartupCoordinator` 的预热优先、池空冷启动回退和激活失败禁止回退；
- Control Plane 启动入口接入上述策略，停止和销毁路径能够清理附件、绑定与预热项；
- 自动补池 Worker 的目标容量、最大数量、Host 内存和网络槽位守卫；
- TTL 回收、`ready` Firecracker 代次健康检查、Worker 重启协调和自动补位；
- Worker 单进程锁、独立服务启停、配置热重载和目标缩容；
- 超时 `claiming` 的状态分类，以及四类超时的代次校验和安全补偿（回滚 + 向前修复）；
- 默认关闭时 CLI、冷启动、快照和恢复链路保持通过。

仍未完成：

- 多用户并发领取、各阶段崩溃注入、快照/预热/冷启动网络竞争和 rootfs 升级 drain 的完整故障矩阵（四类超时补偿 smoke 与 100 轮压力已覆盖主要路径）；
- 性能样本、灰度策略、监控指标和生产部署方式（阶段 6）。

因此当前代码已经形成可手动运行和测试的完整预热链路，但尚未满足生产开启条件，`PREWARM_ENABLED` 继续保持 `0`。

## 3. 设计原则

1. **默认关闭**：`PREWARM_ENABLED=0` 时现有行为不变。
2. **冷启动永远兜底**：预热池没有可用项时立即冷启动，不无限等待 `preparing` 项。
3. **统一启动入口**：快照、预热和冷启动只能由同一个启动协调器选择，不能并行尝试。
4. **用户数据盘单写**：同一用户盘在运行、暂停快照和激活过程中最多有一个有效属主。
5. **物理身份与业务身份分离**：用户操作逻辑实例，Firecracker、Jailer、TAP 和租约按 `runtime_id` 管理。
6. **只领取 `ready`**：Firecracker 进程存在不等于可领取，必须完成 Guest 健康验证。
7. **领取后不回池**：预热项一旦接触用户数据，无论成功还是失败，都不得清洗后分配给其他用户。
8. **失败可补偿**：SQLite 无法跨数据库和 Linux 资源形成单一事务，流程采用带 `operation_id` 的状态机和幂等补偿。
9. **用户请求优先**：内存、网络槽位或 I/O 紧张时暂停补池，不影响真实用户启动。
10. **逐阶段启用**：先完成纯数据库状态机，再接单台人工领取，最后才启动自动 Worker。

## 4. 身份与资源模型

### 4.1 四类 ID

| ID | 含义 | 生命周期 |
|---|---|---|
| `logical_instance_id` | 用户和 API 看到的业务实例 ID | 创建后持续存在 |
| `runtime_id` | 一次物理 microVM 运行身份，定位 VMM、Jail、TAP、日志和租约 | 物理运行资源存在期间 |
| `operation_id` | 一次启动、领取、恢复或停止操作的代次 | 单次操作期间 |
| `activation_id` | 一次预热 Guest 与用户盘绑定的代次 | 预热领取至实例清理 |

代次校验按职责分开，不能互相替代：

| 校验信息 | 使用位置 | 防止的问题 |
|---|---|---|
| `operation_id` | 业务实例状态更新、启动超时与补偿 | 旧启动操作覆盖新一代业务状态 |
| `activation_id` | 预热激活、重连查询与提交 | 重复激活、响应重放或迟到响应 |
| `runtime_id + PID + process_start_ticks` | VMM、Sidecar、Watcher 与清理 | PID 复用后误判或误杀新进程 |

不要求所有现有生命周期调用机械地携带 `operation_id`。业务状态的异步更新必须校验
`operation_id`，物理进程操作继续使用已有的 PID 代次校验；两个层面同时存在。

### 4.2 三种启动策略

```text
用户请求启动
    ↓
已有 running 实例？──── 是 → 直接复用
    ↓ 否
有可恢复的用户快照？── 是 → snapshot
    ↓ 否
有兼容的 ready 项？─── 是 → prewarm
    ↓ 否
普通冷启动 ─────────────────→ cold
```

策略只决定如何得到运行中的 microVM。用户接口、任务协议和最终 `running` 状态保持一致。

## 5. 模块划分

### 5.1 StartupCoordinator：统一启动仲裁

`services/instance-manager/startup_coordinator.py` 已由 Control Plane 调用，避免把策略选择继续堆积到 `server.py`。

职责：

- 对逻辑实例取得启动权；
- 生成 `operation_id`；
- 按 `running → snapshot → prewarm → cold` 选择唯一策略；
- 记录阶段和启动结果；
- 调用失败补偿；
- 保证快照失败时不会静默启动另一台 Guest 并挂载同一数据盘。

它不直接执行 Linux 命令，实际资源操作仍交给现有 `InstanceRuntime`、`NetworkManager` 和预热模块。

### 5.2 PrewarmStore：预热状态存储

`services/instance-manager/prewarm_store.py` 已实现以下职责：

- 创建与查询 `prewarm_items`；
- 使用 `BEGIN IMMEDIATE` 原子领取；
- 条件状态转换；
- 保存健康检查、超时和错误信息；
- 防止同一 `runtime_id` 被重复领取。

状态更新必须集中为条件转换方法，而不是由各调用点自行执行无条件 `UPDATE`。建议提供：

```python
transition_if_current(
    runtime_id,
    expected_status,
    operation_id,
    new_status,
)
```

返回值必须明确表示“本代操作完成转换”或“记录已被其他代次修改”。启动协调器、超时回调、
Worker 和 Reconcile 共用同一套条件转换，避免每个调用点各自实现代次判断。

### 5.3 PrewarmManager：单项编排

`prewarm_manager.py` 作为用户领取侧的单项编排入口，已实现领取、激活、提交和停止后归属清理：

- `claim()`：领取一条 `ready` 记录；
- `activate()`：切盘并执行 Guest 激活；
- `commit()`：确认逻辑实例、物理运行时和数据盘绑定；
- `complete_stop()`：用户实例停止后释放附件和绑定，并退休已分配预热项。

公共池的 `prepare_one()`、健康检查、回收和 reconcile 已归入 `PrewarmWorker`，避免用户请求编排与后台容量维护混在同一模块。

### 5.4 PrewarmWorker：后台补池

`services/instance-manager/prewarm_worker.py` 已实现公共池维护：

- 根据 `target/max/build_concurrency` 和 Host 容量计算补池数量；
- 创建真实空白 Guest，并在数据库提交失败时执行补偿清理；
- 执行 TTL 回收、`ready` 进程代次健康检查和目标缩容；
- Worker 启动时先 reconcile，再进入周期巡检和失败退避；
- 热重载 `config/prewarm.env`，无需重启即可调整目标数量或关闭池；
- 审计超时 `claiming`，按分类自动补偿：回滚未确认挂载/已挂载/已激活的项目，向前修复已提交项目。

Worker 不接收用户请求，也不能直接修改业务实例状态。

相关进程入口为 `prewarm_worker_main.py`，手动服务管理位于
`scripts/linux/prewarm-worker-service.sh`。当前使用 `flock` 保证单 Host 同时只有一个
Worker；它不是多 Host Leader Election，扩展到服务器集群前仍需替换为分布式租约。

### 5.5 超时领取审计与补偿

`prewarm_claim_audit.py` 组合 `prewarm_items`、`startup_operations`、
`volume_attachments` 和 `runtime_bindings`，将超时领取分类为：

- `pre_attach_timeout`；
- `reserved_timeout`；
- `activation_timeout`；
- `commit_pending`；
- `operation_missing/operation_mismatch`；
- `failed_requires_cleanup/terminal_inconsistent/attachment_error/inconsistent`。

`prewarm_claim_compensation.py` 提供三个补偿器：

- `PreActivationClaimCompensator`：回滚 `pre_attach_timeout` / `reserved_timeout`，停 VMM → 销毁 → 释放 reserved 附件（若存在）→ 退休；
- `ActivationTimeoutClaimCompensator`：回滚 `activation_timeout`，额外释放 attached 附件 + 删除 binding；
- `CommitPendingClaimCompensator`：向前修复 `commit_pending`，只补 `mark_assigned` + `mark_succeeded`，不停 VMM。

回滚类先把本代启动操作条件转换为 `compensating`，再重新读取预热项、attachment 和 binding；只有
代次一致、附件/binding 状态匹配时才停止并销毁该 Runtime，最后把预热项转为 `retired`、启动操作转为
`failed`。`commit_pending` 依赖「binding=running ⟹ 业务实例已 running」不变式，直接向前补账。

任何竞争、代次不一致或 `operation_missing`/`commit_incomplete`/`inconsistent` 等异常分类都不会
自动处理，而是记录 `PREWARM_WORKER_CLAIM_DEFERRED`，等待人工检查。

### 5.6 ActivationClient：Host 激活客户端

Host 侧固定协议已封装在 `prewarm_activation.py`，负责：

- `PROBE_V1`；
- `ACTIVATE_V1`；
- `COMMIT_V1`；
- 超时、响应校验和重连；
- 校验返回的 `activation_id` 与预期一致。

Guest 继续使用现有 `scripts/guest/prewarm-activate.sh`，不得接受任意 Shell 命令或 `eval`。

### 5.7 复用现有模块

| 模块 | 预热中的职责 |
|---|---|
| `InstanceRuntime` | 创建/停止物理 VMM、准备 Jailer、热切数据盘、登记运行元数据 |
| `RuntimeStore` | 保存物理运行信息和逻辑/物理绑定 |
| `NetworkManager` | 唯一的网络槽位分配和 Linux 网络配置入口 |
| `Lifecycle/Watcher` | 仅在预热项正式分配后管理用户实例；公共 `ready` 项由 Worker 管理 |
| `Control Plane` | 认证、业务实例状态、调用 StartupCoordinator |

## 6. 数据库设计

### 6.1 现有表继续保留

| 表 | 用途 |
|---|---|
| `instances` | 业务逻辑实例与用户归属 |
| `runtime_instances` | Firecracker PID、Jail、网络端点、数据盘等物理运行状态 |
| `runtime_sidecars` | Host Sidecar 进程 |
| `network_leases` | `/30` 槽位、TAP、MAC、nftables 与租约状态 |
| `runtime_bindings` | 逻辑实例与物理 `runtime_id` 的绑定 |
| `user_volumes` | 用户与唯一数据盘的持久归属 |

普通冷启动 microVM 只进入现有运行表，不进入预热表。预热 microVM 同时拥有 `runtime_instances` 和 `prewarm_items` 记录：前者描述真实资源，后者描述池状态。

### 6.2 新增 prewarm_items

建议字段：

| 字段 | 说明 |
|---|---|
| `runtime_id` | 主键，物理 microVM ID |
| `host_id` | 所在 Host，单机阶段仍保留 |
| `profile_key` | 内核/rootfs/vCPU/内存/网络策略的兼容规格 |
| `status` | `preparing/ready/claiming/assigned/draining/error/retired` |
| `claim_token_hash` | 激活令牌摘要，不保存明文 |
| `logical_instance_id` | 领取前为空，领取后填写 |
| `user_id` | 领取前为空 |
| `volume_path` | 领取前为空 |
| `operation_id` | 本次领取操作代次 |
| `activation_id` | Guest 激活代次 |
| `vmm_pid` | 便于核对，最终仍以 runtime 表为准 |
| `process_start_ticks` | 防 PID 复用 |
| `created_at/ready_at/claimed_at/updated_at` | 阶段时间 |
| `expires_at` | ready 项 TTL |
| `last_health_at` | 最近健康检查 |
| `last_error` | 最近错误摘要 |

必要约束：

- `runtime_id` 唯一；
- 一个 `logical_instance_id` 最多绑定一个未结束预热项；
- `ready` 才允许转为 `claiming`；
- `assigned` 不允许回到 `ready`；
- 过期回调必须同时匹配 `runtime_id + operation_id + 当前状态`。

### 6.3 新增 startup_operations

建议在 Control Plane 数据库增加启动操作记录：

| 字段 | 说明 |
|---|---|
| `operation_id` | 主键 |
| `logical_instance_id` | 目标业务实例 |
| `owner_id` | 用户 |
| `strategy` | `snapshot/prewarm/cold` |
| `status` | `deciding/executing/committing/succeeded/failed/compensating` |
| `runtime_id` | 选定后填写 |
| `created_at/updated_at` | 操作时间 |
| `last_error` | 失败原因 |

该表用于审计与代次隔离，不代替业务实例状态。

必须通过唯一约束或等价的条件事务，保证同一 `logical_instance_id` 同时最多存在一个
`deciding/executing/committing/compensating` 操作。`succeeded/failed` 为历史终态，不阻止
后续新操作。业务实例状态更新时必须同时匹配 `logical_instance_id + operation_id + 预期状态`；
这与 §5.2 的 `transition_if_current` 是同一套条件转换，仅键从 `runtime_id` 换成
`logical_instance_id`，建议提供对称 helper 统一实现，避免业务侧各自手写条件更新。

### 6.4 新增 volume_attachments：数据盘独占

明确采用独立 `volume_attachments` 表，不使用“给 `runtime_bindings` 增加唯一约束”的替代方案。
原因是 `runtime_bindings` 主要表达预热领取后的逻辑/物理绑定，不能完整覆盖普通冷启动和已经
停止 Firecracker 进程的暂停快照。

建议字段：

| 字段 | 说明 |
|---|---|
| `volume_path` | 主键，一块盘最多存在一条活动附件 |
| `owner_id` | 数据盘所属用户 |
| `logical_instance_id` | 占用该盘的业务实例 |
| `runtime_id` | 当前或最近一次物理运行时 |
| `operation_id` | 建立或改变附件状态的操作代次 |
| `status` | `reserved/attached/suspended/releasing/error` |
| `created_at/updated_at` | 创建与更新时间 |
| `last_error` | 最近一次附件错误 |

状态语义：

```text
启动或领取前：reserved
Guest已挂载：attached
快照暂停后：suspended
完全停止/删除快照：releasing → 删除
无法确认是否仍挂载：error（继续保持独占）
```

普通冷启动、预热激活和快照恢复必须通过同一个附件存储取得数据盘独占权。运行实例和暂停
快照都继续占有数据盘；只有实例明确停止且不再保留快照，或者实例销毁后才能释放。快照恢复
失败时仍保持 `suspended`，不能自动让预热或冷启动绕过独占记录挂载同一块盘。

## 7. 状态机

### 7.1 业务实例状态

```text
created/stopped/suspended
          ↓
       starting
          ↓
       running
          ↓
  suspending/stopping
          ↓
 suspended/stopped
```

同一逻辑实例只能有一个未完成的启动操作。建议用条件更新取得启动权，而不是先查询再更新。

### 7.2 预热项状态

```text
preparing → ready → claiming → assigned
    │         │         │
    └─────────┴─────────┴→ error → draining → retired
                  ready ─→ draining → retired
```

- `preparing`：资源正在创建，不可领取；
- `ready`：收到并验证 `WARM_READY`，可领取；
- `claiming`：已被一个操作锁定，正在切盘和激活；
- `assigned`：已经变成用户实例，不再属于公共池；
- `draining`：正在停止和清理；
- `error`：状态或资源异常，只允许检查和清理；
- `retired`：物理资源已经清理完成的终态。

### 7.3 网络租约状态

继续使用现有：

```text
reserved → active → releasing → 删除
                  ↘ error
```

预热项从 `preparing` 开始就占用真实网络槽位；达到 `ready` 前必须已经是 `active`。因此池大小必须同时受内存和可用网络槽位限制。

## 8. 核心流程

### 8.1 后台补池

```text
Worker读取配置
→ 统计兼容profile下的preparing+ready
→ 检查内存、网络槽位、并发和构建状态
→ 写入preparing
→ 申请runtime_id和网络租约
→ 创建TAP、nftables、Jailer和空白Guest
→ 校验PID/start ticks/API Socket
→ 收到WARM_READY
→ PROBE_V1确认未激活
→ 条件更新为ready
```

创建失败时进入 `error`，清理真实资源后进入 `retired`。不能只删除数据库记录而遗留 VMM、TAP 或 nftables。

### 8.2 用户领取与激活

```text
Control Plane取得逻辑实例启动权
→ StartupCoordinator确认无可用运行实例和有效快照
→ 原子领取ready项：ready→claiming
→ 取得数据盘独占权
→ 再次验证VMM代次、网络、激活端点和profile
→ 将占位后端PATCH为用户data.ext4
→ 发送ACTIVATE_V1
→ Guest校验Token和UUID
→ Guest mount + bind mount + 权限写探测
→ Guest启动Pi RPC
→ 返回BOUND_READY
→ Host验证Pi RPC端点
→ 写runtime_bindings
→ 更新业务实例running
→ 发送COMMIT_V1
→ claiming→assigned
```

Host 数据库才是最终归属依据，Guest 的 `BOUND_READY` 只是激活已完成的证据。
「取得数据盘独占权」到「写 runtime_bindings」之间，`volume_attachments` 与
`prewarm_items.volume_path` 必须随同一 `operation_id` 一起更新，避免两表脱节。

### 8.3 没有 ready 项

第一版不等待 `preparing`：

```text
没有ready项 → 立即走现有冷启动
```

未来可以在有充分计时证据后增加最多数百毫秒的等待窗口，但不能让用户请求依赖一个无确定完成时间的后台项目。

### 8.4 停止已分配预热实例

`assigned` 后按普通用户实例停止：停止任务、Pi RPC、Sidecar、Firecracker，清理网络和 Jailer，释放数据盘附件。该物理 Guest 不返回公共池，Worker 另建一台空白项补位。

## 9. 快照、网络和预热的协调

### 9.1 当前真实行为

当前快照清单保存原网络信息。暂停完成后会删除 TAP、路由和 nftables，但把原租约保留为 `reserved`；恢复时 `reserve_snapshot_lease()` 复用同一槽位，再重建网络资源并切回 `active`。

### 9.2 已采用的网络策略

首期已经采用“快照槽位保留”：

- 暂停时删除 TAP、路由和 nftables；
- `network_leases` 保留原槽位，状态维持 `reserved`；
- 恢复时重建相同网络资源并切回 `active`；
- 删除快照或转为完全停止时才释放槽位；
- Worker 统计空闲槽位时必须排除快照保留项。

该策略会占用地址槽位，但避免修改快照内 Guest 网络和恢复竞争。单机 `/16` 切 `/30` 有较多槽位，第一版优先保证正确性。后续如果暂停实例规模导致槽位压力，再设计 Guest 恢复后的网络重配置。

网络预算不能只看地址池的理论容量。`172.29.0.0/16` 按 `/30` 理论上可划分 16384 个槽位，
但实际容量还受内存、TAP、nftables、进程和文件描述符限制。所有以下状态都会消耗槽位：

```text
运行中的用户实例
+ 暂停快照保留的reserved槽位
+ preparing/ready/claiming预热项
```

因此自动补池必须设置“用户请求保底槽位”，预热 Worker 不能使用这部分余量。

### 9.3 启动决策互斥

同一个逻辑实例的顺序固定为：

```text
running复用 → 有效快照恢复 → ready预热领取 → 冷启动
```

快照存在但恢复失败时，实例保持 `suspended` 或进入可诊断错误状态，不允许静默领取预热机，因为快照与数据盘仍是一组受保护资产。

## 10. 失败场景与处理

| 场景 | 处理原则 |
|---|---|
| 用户到达时只有 `preparing` | 不领取，直接冷启动 |
| 两个请求抢一台 `ready` | SQLite 条件更新只允许一个进入 `claiming` |
| 同一用户重复 start | 业务实例启动锁只允许一个 `operation_id` |
| 领取后 VMM 已退出 | 标记 `error`、清理、冷启动兜底 |
| PATCH 前失败 | 释放数据盘附件，销毁预热项，可冷启动 |
| PATCH 后、ACTIVATE 前失败 | 该项不回池；销毁 VMM，检查数据盘后再决定冷启动 |
| Guest 返回错误 UUID | 立即销毁该项，保留审计，不挂载数据盘 |
| `BOUND_READY` 响应丢失 | 使用相同 activation_id 执行 `PROBE_V1`，不得生成第二次绑定 |
| Guest 激活成功、Host 提交失败 | 进入补偿状态；数据盘仍保持独占，reconcile 后提交或销毁 |
| Host 提交成功、COMMIT 响应丢失 | 数据库绑定有效；重连查询，不重复领取 |
| Worker 与 TTL 回收并发 | 回收只允许 `ready→draining`，不能处理 `claiming` |
| `claiming` 超时且尚无 attachment/binding | 条件进入 `compensating`，复查归属与进程代次后停止、销毁并退休 |
| `claiming` 超时但已预留或挂载数据盘 | 只分类并记录 deferred，不自动释放数据盘或停止 Runtime |
| 补偿开始后并发出现 attachment/binding | 立即中止清理，保持隔离状态，禁止误停正在激活的实例 |
| Firecracker PID 或启动代次不匹配 | 硬拒绝停止，记录错误并等待人工或后续 reconcile |
| 预热项健康检查超时 | `ready→error→draining`，不交给用户 |
| 快照原槽位被占用 | 自动预热启用前通过槽位保留解决；否则安全拒绝恢复 |
| Control Plane 重启 | 根据 operation、binding、runtime、lease 和真实 PID 执行 reconcile |
| Worker 重启 | 不凭内存状态补池，先核对 SQLite 和真实资源 |
| 数据盘已被另一 runtime 使用 | 独占约束拒绝激活，不能以冷启动绕过 |
| rootfs/profile 已升级 | 旧 `ready` 项进入 draining，不能分给新请求 |
| Host 内存不足 | 暂停补池；不销毁已分配用户实例 |
| 网络槽位不足 | 停止补池；用户冷启动也返回明确容量错误 |

## 11. Reconcile 规则

系统启动和定期巡检必须以“数据库记录 + PID/start ticks + API Socket + Linux 网络资源 + Guest 探针”共同判断：

- 数据库 `preparing`，但 VMM 不存在：标记 `error` 并清理租约；
- 数据库 `ready`，但 `PROBE_V1` 失败：进入 `draining`；
- 数据库 `claiming` 超时：先由 `PrewarmClaimAuditor` 组合 operation、attachment 和 binding 分类；
- `pre_attach_timeout`：允许 `PreActivationClaimCompensator` 在二次校验和进程代次匹配后回滚清理；
- `reserved_timeout/activation_timeout`：分别回滚，额外释放 reserved/attached 附件、删除 binding；
- `commit_pending`：`CommitPendingClaimCompensator` 向前修复，只补 `mark_assigned` + `mark_succeeded`；
- 其余异常分类（`operation_missing`/`commit_incomplete`/`inconsistent` 等）：只报告，不自动处理；
- 数据库 `assigned`，但没有 binding：进入人工/自动补偿，不回公共池；
- binding 存在但业务实例未 `running`：按 `operation_id` 判断继续提交还是停止；
- 无数据库记录但存在预热 VMM：作为孤儿资源清理，不自动认领；
- 网络租约存在但 VMM、TAP 均不存在：按租约用途决定释放或保留快照槽位。

任何 reconcile 操作都应幂等，重复执行不会产生第二台实例或第二条租约。

## 12. 安全边界

- 预热等待期没有用户数据、Session 和业务 Pi 进程；
- 模型 API Key 继续只保存在 Host，不写入 Guest；
- 激活 Token 使用高强度随机值，数据库只保存摘要；
- 激活端口只允许对应 Host TAP 地址访问；
- `ACTIVATE_V1` 只接受固定字段，不执行任意命令；
- 数据盘 UUID、预期路径和用户归属必须同时校验；
- `activation_id` 防止重放和过期响应覆盖；
- 接触过用户数据的 Guest 永不回公共池；
- 日志不得打印 Token、API Key 或用户文件内容。

## 13. 容量与调度

配置继续以 `config/prewarm.env` 为单一入口：

- `PREWARM_TARGET_READY`：希望长期保持的可领取数量；
- `PREWARM_MAX_TOTAL`：`preparing + ready + claiming` 上限；
- `PREWARM_BUILD_CONCURRENCY`：并行补池上限；
- `PREWARM_TTL_SECONDS`：空白项最大等待时间；
- `PREWARM_MIN_AVAILABLE_MIB`：补池前最低可用内存；
- `PREWARM_MIN_FREE_NETWORK_SLOTS`：补池后仍须留给冷启动和恢复的最小空闲槽位（已实现配置解析与容量查询）；
- `PREWARM_CLAIM_TIMEOUT_SECONDS`：领取事务超时；
- `PREWARM_ACTIVATE_TIMEOUT_SECONDS`：Guest 激活超时。

Worker 的计算原则（两个公式分开）：

```text
ready缺额 = max(0, target_ready - (ready + preparing))
可创建数 = min(ready缺额,
              max_total - (preparing + ready + claiming),
              并发余量,
              内存预算,
              max(0, free_network_slots - min_free_network_slots))
```

- `ready缺额` 不抵消 `claiming`：一台 `ready` 进入 `claiming` 后已不能再服务其他用户，
  Worker 应立即开始补位，而不是等 `claiming` 结束后才补，否则会降低连续请求的命中率。
- `PREWARM_MAX_TOTAL` 计入 `claiming`：`preparing + ready + claiming` 共同受总项数上限约束，
  防止领取高峰时无限并行创建。
- `free_network_slots = 总槽位 − (运行中用户实例 + 快照保留 reserved + preparing/ready/claiming 预热项)`；
  Worker 只能使用扣除用户保底后的这部分余量。

用户启动、快照恢复、rootfs 构建或 Host 高负载时暂停补池。预热池只移动启动成本，不增加服务器能承载的用户总量。

## 14. 可观测性

每次操作至少记录：

- `logical_instance_id/runtime_id/operation_id/activation_id`；
- 选择的启动策略及原因；
- `preparing→ready`、等待、领取、PATCH、激活和提交耗时；
- 冷启动回退原因；
- 池数量、命中率、失败率、TTL 回收数；
- VMM、TAP、nftables、网络租约和数据盘附件清理结果。

建议指标：

```text
prewarm_ready_count
prewarm_preparing_count
prewarm_claim_total
prewarm_claim_conflict_total
prewarm_activation_failure_total
prewarm_fallback_cold_total
prewarm_orphan_cleanup_total
prewarm_prepare_seconds
prewarm_activate_seconds
instance_start_seconds{strategy="cold|prewarm"}
prewarm_rollout_eligible_total
prewarm_hit_total
prewarm_miss_total
prewarm_compensation_total{classification="..."}
```

阶段 6 性能测试必须保留原始样本，运行期指标只用于观察趋势，不能替代可复算的 JSONL/CSV
验收数据。指标标签不得包含 `user_id`、`logical_instance_id` 等高基数字段；具体实例诊断继续写入
结构化日志。

## 15. 分阶段实施

### 阶段 0：冻结基线与明确不变量

- 保持预热默认关闭；
- 固化冷启动、快照、生命周期和恢复压力测试；
- 补充数据盘独占与快照网络槽位策略的单元测试设计；
- 不创建真实预热机。

验收：现有 `cli-smoke-test`、生命周期 100 轮、恢复压力测试和快照测试保持通过。

### 阶段 1：纯数据库状态机

- 实现 `prewarm_items`、`startup_operations` 和 `volume_attachments`；
- 实现条件状态转换和原子领取；
- 提供统一的 `operation_id` 条件转换 helper；
- 用模拟 runtime 做并发测试；
- 不接 Control Plane，不启动 VMM。

验收：多线程/多进程争抢同一项时只有一个成功；同一逻辑实例不能建立两个未完成启动操作；
旧 operation 无法更新新代次；同卷双挂被拒绝；附件处于 `error` 时保持锁定而不是自动释放。

### 阶段 2：统一启动协调器

- 引入 StartupCoordinator；
- 先只路由现有快照与冷启动；
- 行为必须与当前版本一致；

验收：关闭预热时，冷启动和快照的 API、CLI 语义及资源清理行为与当前版本一致。

### 阶段 2A：快照网络槽位保留

- 暂停时删除 TAP、路由和 nftables，但把原租约保留为 `reserved`；
- 快照恢复复用已有租约，不再插入第二条相同槽位记录；
- 完全停止、删除快照或销毁实例时才释放槽位；
- Reconcile 能区分有效快照预留和网络泄漏；
- 增加 `PREWARM_MIN_FREE_NETWORK_SLOTS` 配置解析与容量单测；
- 此阶段预热仍保持关闭。

验收：快照暂停期间其他冷启动不能取得其原槽位；恢复、停止、销毁和异常重启后租约状态正确；
快照、冷启动、生命周期及恢复压力测试全部通过。

### 阶段 3：单台人工预热接管

- 将现有技术探针接入正式 NetworkManager、RuntimeStore 和 PrewarmStore；
- 人工创建一台 `ready` 项；
- 用户请求能够领取、激活、提交和正常停止；
- 暂不启动自动补池。

当前进度：功能实现已完成。Runtime 真实领取、Pi RPC 验证、提交和停止清理专项测试已通过；Control Plane 已接入启动策略和退出清理闭环，默认关闭时 CLI 回归保持通过。连续领取压力验收归入阶段 5 统一执行。

验收：连续至少 30 次真实领取；错误 Token/UUID/重复激活全部硬失败；数据盘和网络无残留。

### 阶段 4：自动补池 Worker

- 实现 target/max、TTL、健康检查、内存门槛和退避；
- Worker 重启先 reconcile；
- 用户请求优先，领取后异步补位。

当前进度：核心功能已完成。已实现容量计划、单台准备、Host 容量探针、后台循环、指数退避、TTL 回收、启动 reconcile、`ready` 健康检查、自动补位、目标缩容、配置热重载、单进程锁和独立服务管理。

已通过真实环境验收：

- `prewarm-worker-smoke-test`：Worker `--once` 创建真实 `ready` 项并安全清理；
- `prewarm-worker-reconcile-smoke-test`：清理中断项并自动补位；
- `prewarm-worker-health-smoke-test`：发现死亡 Firecracker、退休旧项并补位；
- `prewarm-worker-reload-smoke-test`：热重载目标为 `0` 并清空公共池；
- `prewarm-worker-lock-smoke-test`：拒绝并发 Worker 且无副作用；
- `prewarm-worker-service-smoke-test`：独立服务启动、状态、日志与安全停止。

验收：调整目标数量不改代码；Worker 重启无重复项；资源紧张时停止补池；关闭开关能 drain 公共项。

### 阶段 5：故障与并发验证

- 多用户同时领取；
- Control Plane/Worker/Firecracker 在各阶段崩溃；
- PATCH、激活、提交和响应丢失；
- 快照、预热和冷启动同时竞争网络；
- rootfs 升级和旧池 drain；
- 至少 100 轮领取、停止和补位。

当前进度：已完成。超时 `claiming` 只读审计、四类超时安全补偿（`pre_attach_timeout`/`reserved_timeout`/`activation_timeout` 回滚、`commit_pending` 向前修复）和 Worker 周期接入均已落地，并各自通过真实 Firecracker smoke test。100 轮「领取/停止/补位」压力验收通过（`prewarm-pressure-test`）。Instance Manager 当前 200 项单元测试通过。

验收：无同卷双写、无重复绑定、无网络槽位冲突、无孤儿 VMM/TAP/nftables/Sidecar。

### 阶段 6：性能验收与灰度

阶段 6 不再增加新的资源状态，而是回答三个问题：预热到底节省多少用户等待时间、额外占用多少
Host 资源，以及出现性能或正确性异常时能否立即停止新的预热领取。实施顺序固定为“统一计时 →
A/B 测试 → 性能判定 → 灰度控制 → 关闭与回滚”，不能在计时口径不一致时直接宣称性能提升。

#### 阶段 6.1：冻结指标与统一计时

冷启动当前已有 `startupTiming.totalMs` 和 Host/Guest 分阶段计时，但该对象只覆盖
`InstanceRuntime.start()` 内部，不包含 Control Plane 策略决策、业务状态更新和最终提交；预热领取
路径也尚未返回等价结构。因此阶段 6 不改变旧对象的含义，而是新增跨策略的
`startupOperationTiming`。

两个对象并存并明确分工：

| 对象 | 作用 | 兼容策略 |
|---|---|---|
| `startupTiming` | 现有冷启动 Runtime/Guest 内部诊断 | 保持 `{outcome,totalMs,host,guest}` 不变 |
| `startupOperationTiming` | 冷启动与预热统一的端到端比较 | 新增版本化结构，阶段 6 主体使用 |

CLI 和其他调用方迁移时优先读取 `startupOperationTiming.totalMs`，新对象不存在时回退到旧
`startupTiming.totalMs`。在所有调用者完成迁移前，不删除旧对象，也不修改旧 `totalMs` 的语义。

统一操作计时的边界固定为：

```text
开始：Control Plane 已取得实例锁，准备选择启动策略
结束：业务实例已置为 running，启动操作已提交，Pi RPC 可以连接
```

这里的“Pi RPC 可以连接”在阶段 6.1 中严格定义为“Guest 已声明 RPC 监听器就绪”：冷启动收到
`PI_RPC_READY`，预热激活收到 `BOUND_READY`。启动路径不会额外执行 `connect + get_state`，因为
建立 RPC 连接会触发 socat 派生 wrapper 和 Pi Agent 进程，混入 Pi 进程启动与 Session 加载耗时。
Host 首次 RPC 响应作为独立体验指标测量。

测试客户端另外围绕 localhost `POST /start` 记录 `clientObservedMs`，用于覆盖 HTTP 处理和响应
开销。该值属于性能测试样本，不写入服务端 `startupOperationTiming`。

`startupOperationTiming` 的权威结构如下；示例和实现不能省略字段：

```json
{
  "startupOperationTiming": {
    "version": 1,
    "strategy": "cold",
    "totalMs": 0,
    "phases": {
      "decisionMs": 0,
      "claimMs": 0,
      "volumePrepareMs": 0,
      "networkReadyMs": 0,
      "gatewayReadyMs": 0,
      "runtimeReadyMs": 0,
      "drivePatchMs": 0,
      "guestActivateMs": 0,
      "commitMs": 0,
      "unattributedMs": 0
    },
    "milestones": {
      "guestDeclaredRpcReadyMs": 0
    }
  }
}
```

字段语义固定如下：

- `phases` 是互不重叠的阶段增量；不适用的阶段固定为 `0`，不能省略；
- `milestones` 是从统一操作起点开始的累计偏移，不能参与 phases 求和；
- `unattributedMs` 不是底层模块计时的独立阶段，而是 Control Plane 最终计算的余数；
- `unattributedMs = max(0, totalMs - 其余已知 phases 之和)`；
- 除毫秒取整误差外，`sum(phases) = totalMs`；
- 启动响应必须返回实际 `strategy`，性能脚本不能根据配置推断是否命中预热。

九个业务阶段的权威映射为：

| Phase | 冷启动来源 | 预热命中来源 |
|---|---|---|
| `decisionMs` | 启动策略判断 | 灰度资格、策略判断和池可用性判断 |
| `claimMs` | `0` | 原子领取预热项和数据盘附件预留 |
| `volumePrepareMs` | create volume、e2fsck、configure volume | create volume、e2fsck、configure volume |
| `networkReadyMs` | 网络槽位分配和 TAP/nftables 配置 | `0`，公共 Guest 后台准备时已完成 |
| `gatewayReadyMs` | 启动模型网关 | 激活时启动模型网关 |
| `runtimeReadyMs` | Jailer、Firecracker、内核、Guest 初始化和 lifecycle 登记 | `0`，公共 Guest 后台准备时已完成 |
| `drivePatchMs` | `0` | 数据盘链接替换和 Firecracker `PATCH /drives/data` |
| `guestActivateMs` | `0` | `ACTIVATE_V1` 到 `BOUND_READY`，包含 Guest mount/bind 和 RPC 监听启动 |
| `commitMs` | running 状态和 startup operation 提交 | running、Guest COMMIT、binding/prewarm 和 startup operation 提交 |

模型网关和数据盘准备是冷启动与预热共同的用户侧成本。预热主要提前完成网络资源和
`runtimeReadyMs`，不能把 gateway、e2fsck、PATCH 或安全提交从用户侧计时中删除。

跨层产出契约固定为：

| 层级 | 产出职责 |
|---|---|
| `InstanceRuntime` | 自己执行的数据盘、网络、gateway、Runtime、PATCH 和 Guest 激活局部增量 |
| `PrewarmManager` | 原子领取、附件预留及预热提交的局部增量，并向上合并 Runtime 报告 |
| `StartupCoordinator` | 记录实际 strategy、策略决策耗时，携带并汇总下层局部报告 |
| `Control Plane` | 记录统一起点、running 状态提交和最终结束点，生成 `totalMs`、milestones 与余数 |
| 性能测试客户端 | 独立生成 localhost `clientObservedMs`，不回写服务端对象 |

底层 Runtime/Manager 只上报自己拥有的局部增量，不生成最终 `totalMs`，也不计算
`unattributedMs`。局部计时只随本次 `StartupResult` 向上传递，不作为新的资源状态，也不能覆盖
SQLite 中的生命周期权威记录。

阶段 6 指标分为三层：

| 指标 | 定义 | 用途 |
|---|---|---|
| `clientObservedMs` | localhost 发出 `POST /start` 到收到成功响应 | 40% 平台侧主验收指标 |
| `startupOperationTiming.totalMs` | 取得实例锁到 running 与 commit 完成 | 平台内部对照与瓶颈分析 |
| `guestDeclaredRpcReadyMs` | 统一起点到 Guest 声明 RPC 监听就绪 | Runtime/Guest 诊断里程碑 |
| 历史展示耗时 | 打开会话到历史消息可显示 | 辅助体验指标，与 Session/缓存有关 |
| Host 首次 RPC 响应 | 建连、派生 Pi、加载 Session 到 RPC 返回 | 辅助体验指标，不属于 microVM 准备耗时 |
| 模型首字耗时 | 提交消息到收到第一个 `message.delta` | 辅助体验指标，受外部模型和公网影响 |

40% 门槛明确指同一 Host 上 localhost 测得的平台侧启动耗时，不包含终端用户到服务器的公网
往返，不能表述为“线上用户等待时间降低 40%”。历史展示、首次 RPC 和模型首字均不得计入该
门槛；模型首字必须使用真实 SSE `message.delta`，不能把 `model_started` 当作首字。

阶段 6.1 按以下顺序实施，每一步单独提交和验收：

| 步骤 | 实施内容 | 最小验收 |
|---|---|---|
| 6.1A | 定义版本化 timing 数据结构、phase 合并器、余数计算和不变量校验 | 纯单元测试覆盖缺字段、负数、重复 phase 和舍入 |
| 6.1B | 把现有冷启动 `host/guest` 映射到统一 phases，保留旧 `startupTiming` | 冷启动 API/CLI 兼容测试通过 |
| 6.1C | 给预热 Runtime/Manager 增加局部计时并通过 `StartupResult` 向上传递 | fake Runtime/Manager 验证九项字段和值域 |
| 6.1D | Control Plane 生成最终 `startupOperationTiming`、strategy、milestone 和余数 | 冷/热响应结构一致，`sum(phases)=totalMs` |
| 6.1E | CLI 优先读取新对象、缺失时回退旧对象，并执行真实回归 | CLI、Control Plane、预热专项与三轮压力通过 |

阶段 6.1 不实现 rollout，不修改灰度百分比，也不开始正式 30+30 A/B 采样；这些工作分别属于
阶段 6.4 和 6.2，避免计时、灰度和性能脚本一次性跨层变更。

验收：冷启动和预热启动响应均包含可解析的策略、统一总耗时和分阶段计时；现有 CLI、冷启动、
快照、预热和 100 轮压力回归保持通过。

#### 阶段 6.2：开发可重复的 A/B 性能测试

新增独立命令，例如：

```bash
./run.sh prewarm-performance-test 30
```

测试必须使用独立的数据库、状态目录、Control Plane 端口、Worker 锁和日志，拒绝与长期运行服务
共享 `/var/lib/fc/control-plane.db`。测试步骤如下：

1. 记录代码提交、rootfs Build ID、内核、Firecracker 版本、CPU、内存和预热配置；
2. 创建固定测试用户、逻辑实例和持久数据盘，避免把首次创建数据盘混入某一组；
3. 冷启动与预热命中分别执行至少 5 轮不计入结果的热身样本，使文件缓存和服务初始化趋于稳定；
4. 冷启动和预热命中各采集至少 30 个有效样本；
5. 按 `cold → prewarm → prewarm → cold` 或等价交错顺序执行，避免时间漂移偏向某组；
6. 预热样本开始计时前必须确认存在兼容 `ready` 项，后台准备时间不计入用户请求耗时；
7. 每次响应必须核对实际 `strategy`，池空回退样本不能混入预热命中集合；
8. 每轮结束后停止实例并检查 binding、attachment、租约、TAP、nftables、Jailer 和占位盘；
9. 保存所有原始样本，再生成汇总；禁止只保存最后的 P95 数字。

建议输出：

```text
/var/lib/fc/prewarm-performance/
├── samples.jsonl
├── summary.json
├── environment.json
├── control-plane.log
└── worker.log
```

每条样本至少保存 `sampleId/strategy/success/clientObservedMs/totalMs/phases/runtimeId/timestamp`。
环境报告还要记录补出一台 `ready` 的后台准备耗时、空闲预热 VMM 的 RSS/PSS、补池前后 Host
可用内存和占用的网络槽位，用于说明性能收益对应的资源成本。
失败样本不得静默删除；只有明确标记为环境无效并记录原因后才能补采。

验收：脚本可重复运行，冷启动与预热各产生不少于 30 个有效样本，原始数据可重新计算全部汇总，
测试退出后无资源残留。

#### 阶段 6.3：性能与正确性判定

**实施状态：已完成（2026-11-18）。** 性能脚本现已自动计算全部统计指标、
成功率、策略回退率、Guest RPC 就绪里程碑与空闲预热资源成本；任一硬性检查
不满足时以非零状态退出。正式主指标与新版采集链路均已通过真实 Firecracker 验证。

P95 使用固定算法：将有效样本升序排列，取 `ceil(0.95 × N)` 对应的样本；同时报告 P50、P95、
最大值、均值、标准差、成功率和策略回退率。不得仅比较平均值，也不得在没有说明的情况下剔除
慢样本。

初始通过条件：

- `prewarm clientObservedMs P95 <= cold clientObservedMs P95 × 0.60`，即同一 Host 上的
  localhost 平台侧启动耗时至少降低 40%；
- 同时报告 `startupOperationTiming.totalMs` 和 `guestDeclaredRpcReadyMs` 的 P50/P95，用于解释
  平台内部耗时和 Guest 里程碑，但二者不替代主门槛；
- 冷启动和预热启动成功率均为 100%；
- 预热命中样本的实际策略均为 `prewarm`；
- 池空时立即走 `cold`，不能等待正在 `preparing` 的 Guest；
- 测试结束后无 binding、attachment、租约、TAP、nftables、Jailer、Sidecar 和占位盘残留；
- Worker 始终遵守内存和网络保底，不因追求命中率突破容量边界。

报告必须单列“每台空闲预热机资源成本”和按 `target_ready` 推算的 Host 容量影响；即使启动 P95
达标，也不能省略常驻内存、网络槽位和后台补池 CPU/I/O 成本。

如果 `clientObservedMs` 未达到 40%，先比较其与 `totalMs` 的差值，再根据 phases 判断瓶颈是在
数据盘检查、模型网关、Runtime、drive PATCH、Guest 激活还是提交阶段，不允许通过删除必要安全
检查来换取通过。报告标题和结论必须使用“平台侧启动耗时”，不得直接外推为线上用户等待时间。

#### 阶段 6.4：增加可控灰度策略

当前 `PREWARM_ENABLED` 是 Worker 和领取链路共用的全局开关，不能直接满足“只对测试用户开启”。
灰度资格只决定某个用户请求能否领取公共 Guest，不属于 Worker 容量配置。阶段 6.4 新增独立的
`PrewarmRolloutPolicy`，由 `StartupCoordinator` 在 `_run_prewarm()` 前调用；Worker 和直接测试
`PrewarmStore/PrewarmManager` 的底层路径不读取灰度策略。

建议配置为显式互斥模式：

```text
PREWARM_ROLLOUT_MODE=all|allowlist|percentage
PREWARM_ALLOWED_USER_IDS=
PREWARM_ROLLOUT_PERCENT=
```

规则固定为：

- 总开关关闭时禁止领取；
- `PREWARM_ENABLED=1` 时必须显式提供 rollout mode，缺失时安全拒绝配置；
- `mode=all` 不允许同时配置 Allowlist 或百分比；
- `mode=allowlist` 要求名单非空，百分比必须为空；
- `mode=percentage` 要求百分比为 `1～100`，Allowlist 必须为空；
- 百分比使用稳定 `user_id` 哈希，不能逐请求随机；
- 配置热重载失败时保留上一份完整有效快照并报警，不能部分应用。

灰度未命中、池为空或容量不足都立即走现有冷启动，不向用户暴露新的 API 语义。仓库默认配置
继续保持 `PREWARM_ENABLED=0`。现有通过 Control Plane 领取预热项的 smoke test 在显式启用时
补充 `PREWARM_ROLLOUT_MODE=all`；Worker smoke 和直接状态机测试不需要该字段。

灰度日志和指标至少包含：

- eligible、hit、miss、cold fallback 数量；
- 激活失败、补偿和资源清理失败数量；
- 当前 `ready/preparing/claiming/assigned` 数量；
- Host 可用内存和空闲网络槽位；
- 按策略分组的启动 P50/P95。

验收：Allowlist 用户可以命中预热，非 Allowlist 用户保持冷启动；百分比决策稳定，`0` 被配置
校验拒绝、`100` 覆盖全部用户；任何灰度用户都不能绕过单用户配额和数据盘独占。

#### 阶段 6.5：实现动态关闭与回滚

Worker 已支持配置热重载，但 Control Plane 当前只在启动时读取一次 `prewarm.env`。因此现有
`PREWARM_ENABLED=0` 还不是完整的即时关闭开关。应把领取策略封装为可按文件 mtime 安全重载的
共享配置，或提供等价的原子策略快照；配置解析失败时保留上一份有效配置并报警，不能进入半更新状态。

关闭后的不变量：

```text
新请求不再领取 ready
→ Worker停止补池并drain公共ready/preparing
→ 已进入claiming的操作继续提交或补偿
→ assigned用户实例继续运行
→ 池空后的新请求全部冷启动
```

增加真实回滚 smoke test：运行 Control Plane 和 Worker，先确认测试用户命中预热，再动态把开关
改为 `0`，验证后续请求实际策略为 `cold`、公共池最终清空、已运行预热实例不被停止且资源无残留。

验收：无需重启 Control Plane 即可停止新的预热领取；关闭动作不会打断已分配用户实例；重新开启
后 Worker 能按目标安全补池。

#### 阶段 6.6：灰度推进与最终验收

灰度按以下顺序推进，每一级都必须满足性能门槛和零正确性事故后才能升级：

```text
隔离测试环境
→ Allowlist测试用户
→ 5%
→ 20%
→ 50%
→ 100%
```

每一级至少观察 24 小时且取得不少于 100 次有效启动；测试流量不足时延长观察期，直到两项条件
同时满足。
以下任一条件触发立即回滚到 `PREWARM_ENABLED=0`：

- 数据盘双挂、绑定冲突或跨用户数据风险；
- 孤儿 VMM、TAP、nftables、Sidecar 或网络租约持续存在；
- 激活/补偿失败率超过预设阈值；
- Host 内存或网络保底被突破；
- 预热 P95 连续两个观察窗口不优于冷启动；
- 冷启动兜底异常。

最终交付物包括原始性能样本、汇总报告、环境信息、灰度记录、回滚演练记录和生产默认配置。
只有完成动态关闭演练、性能门槛、资源清理检查和至少一轮灰度观察后，才允许讨论把生产环境的
`PREWARM_ENABLED` 从 `0` 改为 `1`。

## 16. 文件规划

| 文件 | 计划 |
|---|---|
| `services/instance-manager/prewarm_config.py` | 已有，继续作为配置解析器 |
| `services/instance-manager/prewarm_manager.py` | 已实现，领取、激活、提交与停止清理 |
| `services/instance-manager/prewarm_store.py` | 已实现，状态机与原子领取 |
| `services/instance-manager/prewarm_worker.py` | 已实现，补池、回收、健康检查与领取补偿调度 |
| `services/instance-manager/prewarm_worker_loop.py` | 已实现，周期巡检、配置热重载与退避 |
| `services/instance-manager/prewarm_worker_main.py` | 已实现，Worker 依赖装配与手动入口 |
| `services/instance-manager/prewarm_worker_lock.py` | 已实现，单 Host Worker 进程锁 |
| `services/instance-manager/prewarm_claim_audit.py` | 已实现，超时领取只读分类 |
| `services/instance-manager/prewarm_claim_compensation.py` | 已实现，四类超时补偿（回滚×3 + 向前修复×1） |
| `services/instance-manager/startup_coordinator.py` | 已实现启动仲裁；阶段 6.1 汇总 strategy、decision 和下层局部计时 |
| `services/instance-manager/startup_operation_timing.py` | 阶段 6.1 新增，定义版本化结构、phase 合并、余数计算与不变量校验 |
| `services/instance-manager/prewarm_rollout_policy.py` | 阶段 6.4 新增，仅负责 Control Plane 领取资格，不进入 Worker 容量配置 |
| `services/instance-manager/prewarm_activation.py` | 已实现，Host 激活协议客户端 |
| `services/instance-manager/runtime_store.py` | 已实现 `volume_attachments`、绑定和代次约束 |
| `services/instance-manager/network_manager.py` | 增加快照槽位保留及预热容量查询 |
| `services/control-plane/server.py` | 已接 StartupCoordinator；阶段 6.1 生成统一计时响应，阶段 6.4/6.5 再接灰度与热重载 |
| `scripts/guest/agent-init.sh` | 已有预热分支，后续只做必要修正 |
| `scripts/guest/prewarm-activate.sh` | 已有技术探针实现，固化协议与安全检查 |
| `scripts/linux/prewarm-smoke-test.sh` | 已有底层探针，后续保留为专项测试 |
| `scripts/linux/prewarm-worker-smoke-test.sh` | 已实现，真实单轮自动补池验收 |
| `scripts/linux/prewarm-worker-service.sh` | 已实现，手动管理 Worker 服务 |
| `scripts/linux/prewarm-worker-*-smoke-test.sh` | 已实现，补池、协调、健康、锁、服务和热重载验收 |
| `scripts/linux/prewarm-performance-smoke.py` / `prewarm-performance-smoke-test.sh` | 已实现，冷启动/预热 A/B 样本采集与汇总（run.sh 命令 `prewarm-performance-test`）|
| `scripts/linux/prewarm-rollout-smoke-test.sh` | 计划（阶段 6.4），Allowlist、动态关闭与回滚验收 |
| `config/prewarm.env` | 已有且默认关闭；阶段 6.4 增加显式 rollout mode 及其对应名单或比例 |

## 17. 发布、关闭与回滚

启用条件：

- 所有阶段验收通过；
- `PREWARM_ENABLED=0` 回归全绿；
- 数据盘独占和快照网络规则已经落地；
- 能够观察池状态和执行 drain。

关闭流程：

```text
PREWARM_ENABLED=0
→ Worker停止创建新项
→ ready/preparing进入draining
→ claiming等待提交或补偿
→ assigned用户实例继续正常运行
→ 公共项清理完成
```

关闭预热不能停止已经分配给用户的实例。发生严重故障时，新的启动请求直接回退现有冷启动路径。

## 18. 交接状态与下一项开发任务

### 18.1 当前可用状态

- 开发分支：`prewarm-pool`；
- 当前功能基线：`f925dd8`（阶段 6.3 性能判定、里程碑与资源成本采样）；
- 总开关：`config/prewarm.env` 中 `PREWARM_ENABLED=0`，不得直接改为生产默认开启；
- 阶段 0、1、2、2A、3、4、5、6.1、6.2、6.3 已完成；
- 阶段 6.1 已完成 `startupOperationTiming` 统一计时（冷/热两侧结构一致，CLI 优先新对象回退旧对象）；
- 最近一次本地验收：Instance Manager 230、Control Plane 42、CLI 18 项单元测试全部通过；
- 真实验收已覆盖 30+30 性能基准、竞态修复回归与新版 3+3 里程碑/资源成本采集。

### 18.2 下一项开发任务

阶段 6.1「统一计时」已完成并各自提交验收：`startupOperationTiming` 版本化结构（9 项 phase +
`unattributedMs` 余数 + `guestDeclaredRpcReadyMs` 里程碑）已由 Control Plane 收口生成，冷启动与
预热命中两侧响应结构一致，CLI 优先读取新对象并回退旧 `startupTiming`；真实回归（`cli-smoke-test`、
`prewarm-control-plane-smoke-test`、`prewarm-pressure-test 3`）通过，instance-manager 228、
control-plane 40、CLI 18 单元测试全绿。

阶段 6.2 与 6.3 已完成。下一项不是立即执行生产灰度，而是对预热池做真实并发
领取验收：多个测试用户同时争抢有限 `ready` 项，验证不会重复分配、不会同卷双写，池空请求
立即冷启动，且补池与用户停止并发时无资源泄漏。并发验收通过后，再实现阶段 6.4 的
“预热资格控制”（指定测试账号/稳定百分比）与 6.5 动态关闭；真正的生产灰度延后到平台上线后执行。

在 Control Plane 支持动态策略重载之前，不把 `PREWARM_ENABLED=0` 描述为完整的即时关闭能力。

### 18.3 接手后的基础检查

```bash
python3 -m unittest discover \
  -s services/instance-manager \
  -p 'test_*.py'

./run.sh prewarm-worker-smoke-test
./run.sh prewarm-worker-reconcile-smoke-test
./run.sh prewarm-worker-health-smoke-test
./run.sh prewarm-worker-reload-smoke-test
./run.sh prewarm-pressure-test 3
```

继续开发前应确认长期运行的 Control Plane 和 Prewarm Worker 已停止，避免专项 smoke test 与正式
`/var/lib/fc/control-plane.db`、网络槽位或 Worker 锁发生竞争。
