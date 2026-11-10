# microVM 预热池设计与实施方案

> 文档状态：设计基线，尚未完成生产接入。
>
> 基线日期：2026-11-10。
>
> 当前结论：空白 Guest 热切数据盘并激活 Pi RPC 的技术探针已经成立；自动补池、原子领取、统一启动仲裁和故障恢复仍未实现。预热功能默认关闭，现有冷启动和快照恢复继续作为可用基线。

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

### 2.2 尚未成立的系统能力

当前还没有完整预热池：

- `PrewarmManager.fill_if_needed()`、`claim()`、`drain()`、`reconcile()` 仍未实现；
- 没有 `prewarm_items` 状态表；
- 没有后台自动补池 Worker；
- Control Plane 尚未执行“快照、预热、冷启动”统一仲裁；
- 没有跨启动方式统一的数据盘独占锁；
- 快照暂停后会释放网络租约，恢复时重新申请原槽位；若该槽位已被预热机占用，恢复会安全失败；
- 预热技术探针使用固定测试网络，不代表已经接入正式 NetworkManager；
- `runtime_bindings` 已有基础结构，但尚未参与真实预热领取。

所以当前代码只能证明底层激活链路，不能直接打开 `PREWARM_ENABLED=1` 投入使用。

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

任何异步回调、超时处理和清理操作都必须校验 `operation_id` 或 `activation_id`，避免旧操作覆盖新一代实例状态。

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

建议新增 `services/instance-manager/startup_coordinator.py`，由 Control Plane 调用，不让 `server.py` 继续堆积资源编排细节。

职责：

- 对逻辑实例取得启动权；
- 生成 `operation_id`；
- 按 `running → snapshot → prewarm → cold` 选择唯一策略；
- 记录阶段和启动结果；
- 调用失败补偿；
- 保证快照失败时不会静默启动另一台 Guest 并挂载同一数据盘。

它不直接执行 Linux 命令，实际资源操作仍交给现有 `InstanceRuntime`、`NetworkManager` 和预热模块。

### 5.2 PrewarmStore：预热状态存储

建议新增 `services/instance-manager/prewarm_store.py`，负责：

- 创建与查询 `prewarm_items`；
- 使用 `BEGIN IMMEDIATE` 原子领取；
- 条件状态转换；
- 保存健康检查、超时和错误信息；
- 防止同一 `runtime_id` 被重复领取。

### 5.3 PrewarmManager：单项编排

现有 `prewarm_manager.py` 作为唯一业务入口，后续实现：

- `prepare_one()`：创建一台空白 Guest；
- `claim()`：领取一条 `ready` 记录；
- `activate()`：切盘并执行 Guest 激活；
- `commit()`：确认逻辑实例、物理运行时和数据盘绑定；
- `retire()`：销毁不可继续使用的池项；
- `reconcile()`：核对数据库与真实资源。

### 5.4 PrewarmWorker：后台补池

建议新增 `services/instance-manager/prewarm_worker.py`。它只负责公共池维护：

- 周期检查 `ready` 数量；
- 在预算允许时补池；
- 健康检查和 TTL 回收；
- 清理 `error`、过期 `claiming` 和失联 VMM；
- Control Plane 重启后执行 reconcile。

Worker 不接收用户请求，也不能直接修改业务实例状态。

### 5.5 ActivationClient：Host 激活客户端

建议把 Host 侧固定协议封装为独立模块，负责：

- `PROBE_V1`；
- `ACTIVATE_V1`；
- `COMMIT_V1`；
- 超时、响应校验和重连；
- 校验返回的 `activation_id` 与预期一致。

Guest 继续使用现有 `scripts/guest/prewarm-activate.sh`，不得接受任意 Shell 命令或 `eval`。

### 5.6 复用现有模块

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

### 6.4 数据盘独占

仅有 `user_volumes` 归属关系不足以阻止同一块盘被两台 Guest 同时挂载。建议增加活动附件记录，或给现有绑定增加等价的独占约束：

```text
volume_path → logical_instance_id → runtime_id → operation_id
```

运行实例和暂停快照都继续持有数据盘所有权。只有实例明确销毁或快照被放弃后才能释放。快照恢复失败时不能自动让预热机挂载同一块盘。

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

当前快照清单保存原网络信息。暂停完成后，生命周期清理会释放真实网络资源和租约；恢复时 `reserve_snapshot_lease()` 必须重新取得原槽位，因为 Guest IP、MAC 和网卡状态已经在快照中。

如果原槽位已经被冷启动或预热机占用，当前恢复会拒绝执行。这是安全行为，但会造成用户无法及时恢复。

### 9.2 接入自动预热前必须选择网络策略

首期推荐“快照槽位保留”：

- 暂停时删除 TAP、路由和 nftables；
- `network_leases` 保留原槽位，状态维持 `reserved`；
- 恢复时重建相同网络资源并切回 `active`；
- 删除快照或转为完全停止时才释放槽位；
- Worker 统计空闲槽位时必须排除快照保留项。

该策略会占用地址槽位，但避免修改快照内 Guest 网络和恢复竞争。单机 `/16` 切 `/30` 有较多槽位，第一版优先保证正确性。后续如果暂停实例规模导致槽位压力，再设计 Guest 恢复后的网络重配置。

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
- 数据库 `claiming` 超时：不直接重新分配，先判断数据盘是否已切换或挂载；
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
- `PREWARM_CLAIM_TIMEOUT_SECONDS`：领取事务超时；
- `PREWARM_ACTIVATE_TIMEOUT_SECONDS`：Guest 激活超时。

Worker 的计算原则（两个公式分开）：

```text
ready缺额 = max(0, target_ready - (ready + preparing))
可创建数 = min(ready缺额,
              max_total - (preparing + ready + claiming),
              并发余量, 内存预算, 网络槽位)
```

- `ready缺额` 不抵消 `claiming`：一台 `ready` 进入 `claiming` 后已不能再服务其他用户，
  Worker 应立即开始补位，而不是等 `claiming` 结束后才补，否则会降低连续请求的命中率。
- `PREWARM_MAX_TOTAL` 计入 `claiming`：`preparing + ready + claiming` 共同受总项数上限约束，
  防止领取高峰时无限并行创建。

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
```

## 15. 分阶段实施

### 阶段 0：冻结基线与明确不变量

- 保持预热默认关闭；
- 固化冷启动、快照、生命周期和恢复压力测试；
- 补充数据盘独占与快照网络槽位策略的单元测试设计；
- 不创建真实预热机。

验收：现有 `cli-smoke-test`、生命周期 100 轮、恢复压力测试和快照测试保持通过。

### 阶段 1：纯数据库状态机

- 实现 `prewarm_items`、`startup_operations` 和数据盘独占记录；
- 实现条件状态转换和原子领取；
- 用模拟 runtime 做并发测试；
- 不接 Control Plane，不启动 VMM。

验收：多线程/多进程争抢同一项时只有一个成功；旧 operation 无法更新新代次；同卷双挂被拒绝。

### 阶段 2：统一启动协调器

- 引入 StartupCoordinator；
- 先只路由现有快照与冷启动；
- 行为必须与当前版本一致；
- 修复快照网络槽位保留规则。

验收：关闭预热时所有 API 和 CLI 行为无变化，快照恢复不会与其他租约冲突。

### 阶段 3：单台人工预热接管

- 将现有技术探针接入正式 NetworkManager、RuntimeStore 和 PrewarmStore；
- 人工创建一台 `ready` 项；
- 用户请求能够领取、激活、提交和正常停止；
- 暂不启动自动补池。

验收：连续至少 30 次真实领取；错误 Token/UUID/重复激活全部硬失败；数据盘和网络无残留。

### 阶段 4：自动补池 Worker

- 实现 target/max、TTL、健康检查、内存门槛和退避；
- Worker 重启先 reconcile；
- 用户请求优先，领取后异步补位。

验收：调整目标数量不改代码；Worker 重启无重复项；资源紧张时停止补池；关闭开关能 drain 公共项。

### 阶段 5：故障与并发验证

- 多用户同时领取；
- Control Plane/Worker/Firecracker 在各阶段崩溃；
- PATCH、激活、提交和响应丢失；
- 快照、预热和冷启动同时竞争网络；
- rootfs 升级和旧池 drain；
- 至少 100 轮领取、停止和补位。

验收：无同卷双写、无重复绑定、无网络槽位冲突、无孤儿 VMM/TAP/nftables/Sidecar。

### 阶段 6：性能验收与灰度

- 同一环境采集冷启动与预热命中各至少 30 个样本；
- 分别统计实例准备、Pi 就绪、历史展示和模型首字；
- 先对测试用户灰度，再逐步提高池大小；
- 保留即时关闭开关和冷启动兜底。

初始目标：预热命中时实例准备耗时 P95 相比冷启动降低至少 40%，且不以牺牲正确性和资源清理为代价。

## 16. 文件规划

| 文件 | 计划 |
|---|---|
| `services/instance-manager/prewarm_config.py` | 已有，继续作为配置解析器 |
| `services/instance-manager/prewarm_manager.py` | 已有骨架，逐阶段实现 |
| `services/instance-manager/prewarm_store.py` | 新增，状态机与原子领取 |
| `services/instance-manager/prewarm_worker.py` | 新增，补池与巡检 |
| `services/instance-manager/startup_coordinator.py` | 新增，统一策略仲裁 |
| `services/instance-manager/prewarm_activation.py` | 建议新增，Host 激活协议客户端 |
| `services/instance-manager/runtime_store.py` | 扩展绑定、附件和代次约束 |
| `services/instance-manager/network_manager.py` | 增加快照槽位保留及预热容量查询 |
| `services/control-plane/server.py` | 只接 StartupCoordinator，不内嵌池细节 |
| `scripts/guest/agent-init.sh` | 已有预热分支，后续只做必要修正 |
| `scripts/guest/prewarm-activate.sh` | 已有技术探针实现，固化协议与安全检查 |
| `scripts/linux/prewarm-smoke-test.sh` | 已有底层探针，后续保留为专项测试 |
| `config/prewarm.env` | 已有，默认关闭 |

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

## 18. 下一项开发任务

下一步只实施阶段 1：`prewarm_items + startup_operations + 数据盘独占 + 原子领取单元测试`。

本阶段不启动自动 Worker、不创建真实预热机、不修改用户启动行为。只有数据库状态机和并发约束经过验证后，才进入统一启动协调器和单台人工接管。
