# microVM 预热池开发计划

> 状态：详细开发计划（v2），取代此前已撤销的 `microVM预热池开发流程.md`，供后续开发使用。
>
> 配套阅读：`docs/microVM预热池实施计划.md`（整体方案与设计推演）。本文聚焦「按什么顺序做、每步改哪些文件、怎么验收」。

---

## 1. 背景与目标

- 现状冷启动约 5 秒，其中 Guest 内核启动约 3 秒；这段工作**与用户无关，只依赖运行规格**（内核/rootfs/几核几G/网络策略）。
- 预热池目标：后台提前启动一批「空白 Guest」，用户请求时「领取 → 绑定用户卷 → 激活」，跳过内核启动。
- 首期验收目标：池命中时「实例准备耗时」P95 相比同条件冷启动下降 ≥40%，且 ≤1.5s（待实测校准）。

**只优化「实例准备耗时」，不承诺 Pi 就绪 / 历史显示 / 首字模型**（这三个指标另有出处，见实施计划 §1.1）。

---

## 2. 硬性原则（本次吸取的教训，必须遵守）

1. **默认关闭，冷启动永远兜底**。任何一步失败都不影响现有冷启动路径。
2. **现有冷启动路径一个字不改**。`scripts/guest/agent-init.sh` 的 `managed_runtime` 分支（socat 监听循环）、`scripts/guest/pi-rpc-session.sh` 全程保持原样。
   - 上一次失败的直接原因：把 socat 监听循环从 `agent-init.sh` 抽成独立脚本「复用」，触动了冷启动路径，引发 session 落盘回归。
   - 本次纠正：预热激活脚本自带一份**独立**的监听循环（允许少量重复），冷启动那份完全不动。
3. **每步验收以既有 smoke 全绿为回归门**。尤其 `./run.sh cli-smoke-test` 必须在每一步之后保持 PASS。
4. **一步一验收**，验收不过不进入下一步。所有预热代码用 `agent_prewarm=1` 开关隔离。

---

## 3. 架构总览

```
后台 pool worker ──持续补池──▶ 空白 Guest（内核已 boot、rootfs 已挂、网络已配、
                              /dev/vdb 占位盘未挂、无 Pi、无模型网关）
                                          │ 用户请求计算资源
                                          ▼
                    决策顺序：复用运行 → 快照恢复 → 预热领取 → 冷启动
                                          │ 领取
                                          ▼
                 PATCH /drives/data 热切盘（占位盘 → 用户卷）
                                          │
                                          ▼
                 一次性激活（TCP 8081，ACTIVATE_V1 固定协议）
                                          │
                                          ▼
                 挂载用户卷 → 起 Pi RPC → BOUND_READY → 业务可用
```

- **ID 映射**：`logical_instance_id`（业务实例）/ `runtime_id`（物理 Guest）/ `activation_id`（一次绑定）。
- **存储**：`prewarm_items`（物理项状态机）+ `runtime_bindings`（逻辑↔物理绑定）。
- **数量旋钮**：`config/prewarm.env` 的 `PREWARM_TARGET_READY` / `PREWARM_MAX_TOTAL`，改数量不改代码。

---

## 4. 分阶段计划

### 阶段 0：配置与接口骨架（纯新增，零行为变化）

**目标**：把「数量旋钮」和「PrewarmManager 接口」先立起来，`PREWARM_ENABLED=0` 时对现有系统零影响。

**改动（全部新增，不碰任何现有文件）**：
- 新增 `config/prewarm.env`：`PREWARM_ENABLED=0`、`PREWARM_TARGET_READY=1`、`PREWARM_MAX_TOTAL=2`、`PREWARM_BUILD_CONCURRENCY=1`、`PREWARM_TTL_SECONDS=900`、`PREWARM_HEALTH_INTERVAL_SECONDS=5`、`PREWARM_CLAIM_TIMEOUT_SECONDS=30`、`PREWARM_ACTIVATE_TIMEOUT_SECONDS=10`、`PREWARM_MIN_AVAILABLE_MIB=2048`。
- 新增 `services/instance-manager/prewarm_config.py`：`PrewarmConfig.from_env()` 读取+校验以上字段。
- 新增 `services/instance-manager/prewarm_manager.py`：接口骨架，方法先返回空态/`NotImplementedError`。

```python
class PrewarmManager:
    def target_ready(self) -> int: ...       # 池目标数（读配置）
    def current_ready(self) -> int: ...      # 当前可领取数
    def fill_if_needed(self) -> None: ...    # 缺额补池
    def drain(self) -> None: ...             # 停止接纳、清理未分配项
    def claim(self, profile, logical_instance_id, user_id, volume_path) -> ClaimResult: ...
    def reconcile(self) -> None: ...         # 重启后核对
```

**验收**：
1. `./run.sh cli-smoke-test`、`lifecycle-smoke-test`、`snapshot-smoke-test` 全 PASS（证明开着 `PREWARM_ENABLED=0` 零影响）。
2. 单测：`PrewarmConfig.from_env` 对缺字段/非法值报错；`target_ready()` 返回配置值。
3. 手动确认：`prewarm-status`（预留入口）只读配置、不产生任何 VMM/TAP/租约。

**完成条件**：配置与接口存在、默认关闭零行为、现有回归全绿。

---

### 阶段 1：技术探针（PATCH 热切盘 + 一次性激活）—— 命门

**目标**：先做一台专用探针、不建自动池，证明「空白 Guest 停住 → `PATCH /drives/data` 切盘 → 一次性激活挂载 → Pi RPC READY」。这是整个方案最重要的底层前提，**不过不进入后续阶段**。

**Guest 侧（关键：不重构冷启动，只新增分支和新文件）**：
- `scripts/guest/agent-init.sh`：
  - 在 `cmdline_value` 定义之后、数据盘挂载之前，新增 `prewarm="$(cmdline_value agent_prewarm || true)"`。
  - 数据盘挂载块不动；预热启动参数**不设置** `agent_data_disk=1`，天然跳过挂载（无需改挂载块本身）。
  - 网络配置条件追加 `|| "${prewarm}" == "1"`（最小改动，仅增加一个条件分支）。
  - `managed_runtime` 的 socat 监听循环**保持原样、一个字符不改**。
  - 在脚本末尾新增：`if [[ "${prewarm}" == "1" ]]; then ... 启动激活监听 + 输出 WARM_READY ... fi`。
- 新增 `scripts/guest/prewarm-activate.sh`：一次性激活入口（root 持有，socat EXEC 启动），协议 `ACTIVATE_V1 <claim_token> <activation_id> <expected_volume_uuid>`，校验 token/UUID/重复激活，挂载 + chown + **自带一份 socat 监听循环**起 Pi RPC + 回 `BOUND_READY`；状态存 `/run/prewarm` 防响应丢失重置；无 `eval`。
- `scripts/linux/build-agent-rootfs.sh`：安装 `prewarm-activate.sh` 并纳入 `build_id`。

**Host 侧**：
- 新增 `scripts/linux/prewarm-smoke-test.sh`：占位盘 → 起空白 Guest（boot_args 含 `agent_prewarm=1` + 网络 + `agent_activate_port=8081` + `agent_prewarm_token`）→ 等 `WARM_READY` → `PATCH /drives/data` 把后端从占位盘切到测试 ext4 → 发 `ACTIVATE_V1` → 等 `BOUND_READY` → Pi RPC `get_state` → 停 VM 校验数据标记。

**验收**：
1. `prewarm-smoke-test`：`WARM_READY`（占位盘未挂）、错误 token/UUID/重复激活均被拒、`BOUND_READY`、Pi RPC 可用、数据标记保留、连续 ≥30 次替换成功。
2. **`./run.sh cli-smoke-test` 必须保持 PASS**（这是本轮是否动到冷启动的硬门槛）。
3. 错误输入不执行任意命令、不重复挂载、不碰其他卷。

**完成条件**：≥30 次替换成功 + 错误输入被拒 + cli-smoke-test 仍绿。

---

### 阶段 2：runtime_id 映射拆分（预热仍关闭）

**目标**：引入「逻辑实例 → 物理 runtime_id」映射，让**冷启动先完整走一遍新映射**，预热仍关闭。先解身份耦合，再谈领池。

**改动**：
- `runtime_store.py` 新增 `runtime_bindings` 表：`logical_instance_id, user_id, volume_path, runtime_id, activation_id, 状态, 更新时间`。
- `runtime.py` 的 `paths/status/start/stop/destroy`、`lifecycle.py`、`network_manager.py`：物理资源按 runtime_id 定位，用户盘按 user_id。
- 旧实例迁移：物理 ID 先等于原逻辑 ID。

**验收**：
1. `PREWARM_ENABLED=0` 下 `start → status → chat → heartbeat → busy → stop → destroy` 全链路行为与数据一致。
2. 空闲回收、控制面重启、VMM 崩溃清理、已有快照/恢复路径全回归通过。
3. 单测：`runtime_bindings` 唯一约束生效（一个逻辑实例至多一个物理绑定、一个 runtime_id 至多一个逻辑实例）。

**完成条件**：预热关闭时行为与数据一致，生命周期清理使用正确物理资源。

---

### 阶段 3：SQLite 原子领取与卷独占

**目标**：新表、条件状态更新、activation_id、领取超时与恢复核对；先用模拟 VMM 测并发，再接阶段 1 探针。

**改动**：
- 新增 `services/instance-manager/prewarm_store.py`：`prewarm_items` 状态机（preparing/ready/claiming/assigned/draining/error/retired）+ `BEGIN IMMEDIATE` 原子领取。
- `prewarm_manager.py`：实现 `claim()` / `reconcile()`。

**验收**：
1. 两个用户抢一台 ready → 仅一人命中，另一人冷启动。
2. 同用户同时两次 start → 至多一个有效运行绑定。
3. 同卷已被运行 Guest 使用 → 新接管被拒。
4. 领取时 worker 正在回收 / 进程在事务提交后退出 → 不重复分配、无同卷双写、无过期回调覆盖新状态。

**完成条件**：无重复领取、无同卷双写、无过期回调覆盖新状态。

---

### 阶段 4：接入 Control Plane 与正常生命周期

**目标**：决策顺序「复用运行 → 快照恢复 → 预热领取 → 冷启动」，绑定后登记原有 watcher。

**改动**：
- `server.py`：`start()`/`ensure_running()` 内部策略路由；领取成功后登记 watcher、持久化 sidecar/activity。
- 衔接：`suspended → resume` 必须排在「领池」之前；ready 项走 TTL、**不登记** 300s idle watcher。

**验收**：
1. CLI 无需新命令；同一实例名在「预热命中」与「冷启动」两种路径下业务等价。
2. 池未命中 → 冷启动；接管失败 → 按可核实资源状态决定是否兜底，不重叠启动。
3. 停止后资源完整清理，后台补一台新空白项。

**完成条件**：两种启动路径业务等价，停止后资源完整清理。

---

### 阶段 5：补池 worker 与可配置数量

**目标**：单 worker 补池、target/max、TTL、退避、内存门槛、版本 drain、重启 reconcile；池数量完全由 `PREWARM_TARGET_READY` / `PREWARM_MAX_TOTAL` 驱动。

**改动**：
- 新增 `services/instance-manager/prewarm_worker.py`：单 Host 维护者，`fill_if_needed()` 循环。
- `scripts/linux/control-plane-service.sh`：管理 worker 启停。
- `prewarm_manager.py`：实现 `fill_if_needed()` / `drain()`。

**验收**：
1. 改 `PREWARM_TARGET_READY=1/2`，`prewarm-status` 的 ready 数随之变化（验证「改数量不改代码」）。
2. TTL 到期 → draining → retired，池不无限增长。
3. 领取后自动补位；worker 崩溃/重启不产生重复维护者，不影响已绑定用户任务。
4. Host 内存不足 / 构建 rootfs → 暂停补池，用户请求优先。

**完成条件**：池不无限增长、不干扰用户任务、重启无重复补池、改 env 即改池大小。

---

### 阶段 6：性能验收与快照协同

**目标**：冷热对照、多用户并发、故障测试；用户快照完善后复核专属恢复优先级；公共模板克隆另设门槛。

**验收**：
1. 同条件冷启动 vs 预热命中，采集「实例准备耗时」P50/P95/最大/失败数各 ≥30 样本；命中 P95 降 ≥40% 且 ≤1.5s（实测后校准）。
2. 至少 100 次领取/停止循环，检查资源泄漏（VMM/TAP/nftables/租约/快照）。
3. 多用户与故障矩阵逐条通过（见实施计划 §15.4）。
4. 用户有 suspended 快照时不领公共池使用其配对卷。

**完成条件**：有明确优化幅度、已知限制、关闭步骤和回归证据。

---

## 5. 关键风险与应对

| 风险 | 应对 |
|---|---|
| `PATCH /drives/data` 热切盘后 Guest 读不到新后端 | 阶段 1 探针首先验证；必要时加 Guest 侧 re-probe/清缓存 |
| 并发领取/卷双写 | 阶段 3 `BEGIN IMMEDIATE` 原子领取 + 唯一约束 + fencing（超时只审计不自动重分配）|
| 动到冷启动路径引发回归 | 硬性原则第 2 条：冷启动路径一字不改；每步以 cli-smoke-test 为门 |
| 快照/预热互斥（同卷双写）| 决策顺序「快照恢复优先于领池」；快照失败不静默借池 |
| 预热真实资源成本 | 默认关、小池、TTL、内存门槛；用户请求优先 |

---

## 6. 验收总原则

1. 每步验收都先在**预热默认关闭**前提下跑一遍既有 smoke（cli/lifecycle/snapshot），确认无回归。
2. 涉及磁盘/快照/卷的步骤用专用测试账户与测试卷，**不用业务用户数据**做第一次实验。
3. 每步完成即提交，提交信息带中文分类前缀；未完成不进入下一步。
4. 「数量旋钮」和「`PrewarmManager` 接口」在阶段 0 就立好，后续都通过它操作。

---

## 7. 建议的第一项实际开发任务

按实施计划 §18 与本文阶段 1：先做**单项技术探针**——一个没有用户数据的 Guest 在等待点停住，证明已注册 data 设备能切到测试卷，并经一次性激活完成正确挂载与 Pi RPC READY。探针通过后再依次做 runtime_id 映射、SQLite 原子领取、Control Plane 接入、补池与性能验收。
