# Pi 常驻进程与 Session 加载优化调研

> 本文记录对「Pi RPC 常驻进程」可行性、官方协议能力，以及后续如何改造的调研结论。
> 面向对象：后续接手本仓库的 agent，以及上下层协作的开发者。
> 所有结论均基于：当前仓库代码（`services/pi-gateway/gateway.py`、`scripts/guest/*.sh`）与
> `@earendil-works/pi-coding-agent@1.0.0` 官方文档（`docs/rpc.md`、`docs/rpc-commands.md`）的实测/原文。

---

## 1. 背景与问题

打开旧会话（继续既有 conversation）的耗时主要来自「Pi 进程加载 Session」。实测样本：

| 阶段 | 耗时 |
| --- | --- |
| microVM 冷启动（宿主计时） | 约 `5443 ms` |
| Guest 启动（内核计时，含在内） | 约 `4060 ms`（其中内核启动 `3230 ms`） |
| **Pi 进程加载 Session** | 约 `7879 ms` |

本文聚焦最后一行：为什么「读一个原生 Session」要 7 秒以上，以及能否通过「让 Pi 常驻」消掉它。

核心结论先行：**这 7 秒大头不是「解析 Session JSONL」，而是「冷启动一个全新的 pi（Node.js）进程」。**

---

## 2. 当前实现链路（逐层）

```text
Host Pi Gateway (gateway.py)
  └─ 每发一次请求，建一条 TCP 连接到 Guest 8080
       └─ Guest socat TCP-LISTEN:8080 EXEC:/usr/local/sbin/pi-rpc-session
            └─ 每接受一条连接，socat 就 fork + exec 一个 pi-rpc-session.sh
                 └─ 脚本 exec 一个全新的 pi --mode rpc 进程
                      └─ 连接关闭 → pi 收到 EOF → 退出、被回收
```

关键点：**一个 TCP 连接 = 一个 pi 进程，用完即退**。因此「读历史」和「发消息」每次都要重新付一遍 Node.js + Pi 的冷启动。

### 2.1 Guest 侧脚本

`scripts/guest/agent-init.sh`（启动 socat 监听，每连接 EXEC）：

```bash
socat "TCP-LISTEN:${rpc_port},bind=${rpc_bind_ip},reuseaddr" EXEC:/usr/local/sbin/pi-rpc-session
```

`scripts/guest/pi-rpc-session.sh`（每连接执行一次）：

- 以非 root 的 `pi` 用户运行，设置 `HOME=/home/pi`、`PI_OFFLINE=1`、`PI_SKIP_VERSION_CHECK=1`、`PI_TELEMETRY=0`；
- pi 启动参数固定为：
  `--offline --no-approve --no-extensions --no-skills --no-prompt-templates --no-context-files --tools read,write --mode rpc`，并按需追加 `--provider` / `--model`；
- 读取连接上的**第一条** JSONL 记录：
  - 提取 `workspace` 字段 → `cd /workspace/workspaces/<workspace_id>`（即 workspace = pi 的 cwd）；
  - 提取 `session` 字段 → 在 `/home/pi/.pi/agent/sessions/**/*_<session_id>.jsonl` 中 glob 找文件，
    有则 `--session <file>`，无则 `--session-id <id>`；
  - 把这两个平台扩展字段从记录里**删掉**，再交给 pi；
- 最后 `{ printf 首条; cat } | pi`：把首条 + 连接后续所有输入都转发给同一个 pi 的 stdin。

> 注意：wrapper 之所以要把 `session` 从请求里抠出来转成 CLI 参数，是因为
> **pi 的 `get_messages`/`prompt` 命令本身不接收 session 字段**（见 §4 实测）。

### 2.2 Host 侧网关

`services/pi-gateway/gateway.py`：

- `execute(host, port, prompt, on_event, workspace_id, session_id)`：建一条连接，发一条 `prompt`，读事件直到 `agent_settled`，**关闭连接**；
- `get_messages(host, port, workspace_id, session_id)`：建一条连接，发一条 `get_messages`，读回消息，**关闭连接**。

两者**每次都新建连接**，因此每次都触发 Guest 侧新起一个 pi 进程。

### 2.3 控制面

`services/control-plane/server.py` 的 `_native_conversation_messages()` 用
`pi_gateway.get_messages(guest_ip, rpc_port, workspace_id, session_id)` 读历史，
其耗时被记为 `historyTiming.piSessionLoadMs`（即 CLI 显示的「加载 Pi 会话花费 X.X 秒」）。

数据库 `conversations` 表存的是 `pi_session_id`（= conversation id），**不存** session 文件路径。

---

## 3. 为什么慢：计时口径与证据

### 3.1 `piSessionLoadMs` 实际测的是什么

`server.py` 里的埋点只包住一次 `get_messages` 调用：

```python
history_started_ns = time.monotonic_ns()
source = self.pi_gateway.get_messages(guest_ip, rpc_port, workspace_id, session_id)
history_timing = {"piSessionLoadMs": ...}
```

所以它覆盖的是：**TCP 建连 → socat 起 pi-rpc-session → 起 pi → Node 启动 → Pi 初始化 → 读 Session → 返回**，
是一整条「进程冷启动 + 会话加载」链路，不是单纯「读文件」。

### 3.2 大头是 Node + Pi 冷启动，不是 Session 解析

证据：本仓库 README「microVM 冷启动优化」一节记录，Guest 每次启动时重复执行
`node --version` + `pi --version` 的耗时约为 `6.45 s`，后来挪到 rootfs 构建阶段才消掉。

也就是说，**在这个嵌套虚拟化环境里，光是把 Node + Pi 进程拉起来并让它跑一个版本检查就要约 6 秒**。
所以读历史时每次现起 pi，这 6~8 秒的主体是「进程冷启动」，Session JSONL 只占零头。

### 3.3 快照为什么救不了这笔

Firecracker 全量快照保存的是 Guest 的**完整内存 + vCPU + 设备状态**。原理上，如果 suspend 时
Guest 里有一个正在运行的 pi 进程，它是可以被冻结进快照、resume 后继续跑的。

但当前 pi 是「每连接现起现退」的，suspend 发生在实例空闲时（此时连接已关闭、pi 已退出），
所以内存里**没有 pi 可冻**。这是设计使然，不是快照的技术限制。因此快照只省 microVM 开机，
省不了 pi 冷启动。

---

## 4. 官方文档调研结论（pi 1.0.0）

### 4.1 rpc.md 关键原文

- **「RPC mode runs Pi as a long-lived subprocess controlled through JSON records on stdin and stdout.」**
  —— pi 的 rpc 模式**本就被设计为长驻子进程**，用 stdin/stdout 上的 JSONL 控制。
- 启动：`pi --mode rpc --no-session`。
- 会话相关 CLI 选项：`--provider`、`--model`、`--name`、`--no-session`、`--session-dir`。
- 每条命令可带可选 `id`，响应会回带同 `id`；命令处理是**异步**的，按 id 关联而非按顺序。
- 关闭方式：**关闭子进程 stdin** 即请求有序退出。

### 4.2 rpc-commands.md 完整命令表（按官方章节）

| 分组 | 命令 |
| --- | --- |
| Prompting | `prompt`、`steer`、`follow_up`、`abort`、`clear_queue`、`new_session` |
| State | `get_state`、`get_messages` |
| Model | `set_model`、`cycle_model`、`get_available_models` |
| Thinking | `set_thinking_level`、`cycle_thinking_level`、`get_available_thinking_levels` |
| Queue modes | `set_steering_mode`、`set_follow_up_mode` |
| Compaction | `compact`、`set_auto_compaction` |
| Retry | `set_auto_retry`、`abort_retry` |
| Bash | `bash`、`abort_bash` |
| Session | `get_session_stats`、`export_html`、`switch_session`、`fork`、`clone`、`get_fork_messages`、`get_entries`、`get_tree`、`get_last_assistant_text`、`set_session_name` |
| Discoverable | `get_commands` |

**关键命令中没有 `cd` / `set_workspace` / `set_cwd`** —— 即工作目录（workspace）不能在进程内切换。

### 4.3 关键命令 schema（原文摘录）

#### switch_session（会话切换，本方案核心）

```json
{"type": "switch_session", "sessionPath": "/path/to/session.jsonl"}
```

```json
{"type": "response", "command": "switch_session", "success": true, "data": {"cancelled": false}}
```

**入参是 `sessionPath`（文件路径），不是 session id。** 可被 `session_before_switch` 扩展事件取消。

#### new_session（新建会话）

```json
{"type": "new_session"}
```

可选 `parentSession` 追踪父会话：

```json
{"type": "new_session", "parentSession": "/path/to/parent-session.jsonl"}
```

响应：`{"command":"new_session","success":true,"data":{"cancelled":false}}`。

#### get_messages（读当前会话全部消息）

```json
{"type": "get_messages"}
```

响应：`{"command":"get_messages","success":true,"data":{"messages":[...]}}`，消息为 `AgentMessage` 对象。
**不接收 session 字段，读的是「当前会话」。**

#### get_entries（增量读条目，带游标）

```json
{"type": "get_entries"}
{"type": "get_entries", "since": "abc123"}
```

响应：

```json
{
  "command": "get_entries", "success": true,
  "data": {
    "entries": [
      {"type": "message", "id": "def456", "parentId": "abc123", "timestamp": "...", "message": {"role": "user", "...": "..."}}
    ],
    "leafId": "def456"
  }
}
```

要点：

- 按**追加顺序**返回（不含 session header）；
- 条目 id 是**稳定游标**：传 `since` 只取严格在其之后的条目，可跨客户端重启续读；
- 与 `get_messages` 不同，**包含压缩前历史和废弃分支**；
- `leafId` 是当前叶子条目 id（空会话为 `null`），可用于一次往返判断活跃分支是否移动；
- `since` 不匹配任何条目 id 时返回 `success: false`。

#### get_tree（会话树）

```json
{"type": "get_tree"}
```

返回 `{tree: [...], leafId}`，每个节点为 `{entry, children, label?, labelTimestamp?}`。

#### get_state（当前会话状态）

```json
{"type": "get_state"}
```

返回 `sessionFile`、`sessionId`、`sessionName`、`messageCount`、`pendingMessageCount`、`isStreaming`、`isCompacting`、`model`、`thinkingLevel` 等。

#### prompt（发消息）

```json
{"id": "req-1", "type": "prompt", "message": "Hello, world!"}
```

响应：`{"command":"prompt","success":true,"data":{"disposition":"started"}}`。
`disposition` 取值：`started`（已接受开始 run）/ `queued`（运行中被排队）/ `handled`（被扩展或输入处理器消费）。
流式中发消息需带 `streamingBehavior`（`steer` / `followUp`），否则报错。

#### fork / clone（分支与克隆）

```json
{"type": "fork", "entryId": "abc123"}
{"type": "clone"}
```

`fork` 从活跃分支的某条用户消息分叉，返回被分叉消息文本；`clone` 复制当前活跃分支到新会话。

---

## 5. 实测验证记录

环境：macOS，Node `v23.11.0`（≥ 官方最低 `22.19`），`@earendil-works/pi-coding-agent@1.0.0`。
`get_messages` 不调模型，故无需网关与 microVM。

### 测试 1a：CLI 参数绑定 session（对照组）

```bash
printf '{"id":"1","type":"get_messages"}\n' | pi ... --mode rpc --session-id conv-A
```

输出含 `Warning: No project session found with id 'conv-A'; creating a new session...`，
随后 `get_messages` 返回空 —— 说明 `--session-id` 确实被识别并创建会话。

### 测试 1b：请求字段带 session（关键组）

```bash
printf '{"id":"1","type":"get_messages","session":"conv-A"}\n' | pi ... --mode rpc --no-session
```

输出**没有** `creating a new session` 警告，直接返回空 —— 说明 **`get_messages` 请求里的
`session` 字段被静默忽略**，会话只能通过 CLI 参数（或 `switch_session` 命令）指定。

### 测试 2：单进程持久循环

```bash
{ printf '{"id":"1","type":"get_messages"}\n'; sleep 1; printf '{"id":"2","type":"get_messages"}\n'; } | pi ... --mode rpc --session-id conv-A
```

输出两条 `response`（id=1、id=2）—— 说明 **pi rpc 是一个持久循环**，一个进程能连续处理多条命令。

### 结论

- pi rpc 是持久循环 ✅
- 会话不能靠「请求字段」指定，但能靠 `switch_session` 命令切换 ✅
- 因此「一个常驻 pi 进程服务多个会话」**可行**。

---

## 6. 核心结论

1. **pi rpc 本命用法就是长驻子进程**，当前平台「每连接现起 pi」是没有用上它的长驻能力。
2. **会话切换靠 `switch_session`，入参是 `sessionPath`（文件路径）**；新建靠 `new_session`。
3. **workspace（cwd）无法在进程内切换**（无 `cd`/`set_workspace` 命令）。读历史与会话无关、与 cwd 无关；
   但发消息（`prompt`）要在正确 workspace 下执行文件读写，因此写路径的常驻需要「每 workspace 一个进程」。
4. **`get_messages` 返回全部消息、无分页**；`get_entries` 支持 `since` 游标增量读 + `leafId`，
   更契合平台现有的分页历史模型。
5. 平台目前存的是 `piSessionId`（= conversation id），**不存 session 文件路径**；而 `switch_session`
   需要路径，这是改造必须补上的映射。

---

## 7. 改造方案

### 7.1 阶段 1：常驻 pi 服务「读路径」（消除「打开旧会话」6s）

目标：打开旧会话不再冷启动 pi。

- **Guest 侧**（`scripts/guest/agent-init.sh` / `pi-rpc-session.sh`）：
  改为启动一个常驻 `pi --mode rpc --no-session`（或 `--session-dir /home/pi/.pi/agent/sessions`），
  不再「每连接 EXEC」、不再用 `--session` 绑定会话。
- **Host 侧**（`services/pi-gateway/gateway.py`）：
  持一条长连接；新增 `switch_session`、`get_entries` 方法；`get_messages` 改为走常驻连接，
  读历史前先 `switch_session`。
- **读历史接口**（`server.py`）改为用 `get_entries` 的 `since` 游标增量拉取，替代
  「`get_messages` 全量加载 → Python 再切片分页」。
- **写路径（发消息）暂不动**：它那一次冷启动被模型延迟掩盖，不在本期优化范围。

改动量：Guest 两个脚本 + `pi-gateway.py`（+ 可能的 `server.py` 读历史改游标）。

### 7.2 阶段 2（可选）：写路径常驻

因为 cwd 不可切换，写路径常驻需「**每个 workspace 一个常驻 pi 进程**」（或按活跃 workspace 做小进程池），
进程内用 `switch_session` 切会话。大多数用户只有 1~2 个 workspace，池子很小。

### 7.3 快照配合

若走「常驻 pi + 快照」，需满足：suspend 时 pi 处于空闲等待态。现有 suspend 前置校验
（`instance_has_active_tasks` 有活动任务即 409 拒绝）已保证这一点，无需额外改造。
快照文件大小不变（全量 RAM 镜像，与 pi 是否常驻无关）。

---

## 8. 待决策事项

1. **session id → sessionPath 映射**（阶段 1 必做，三选一）：
   - 推荐：会话创建后用 `get_state` 拿到 `sessionFile`，存进 `conversations` 表；
   - 或：保留薄 wrapper，网关传 session id，Guest 内 glob 出路径再发 `switch_session`；
   - 或：每次 `get_state` 现查（多一次往返，不划算）。
2. **长连接生命周期**：网关需处理空闲超时、断线重连、pi 崩溃自愈（失去「每连接现起」的天然自愈）。
3. **写路径 workspace 粒度**：阶段 2 的进程池上限、LRU 淘汰策略。

---

## 9. 附：与现有代码的差异点速查

| 项 | 现状 | 改造后 |
| --- | --- | --- |
| pi 生命周期 | 每 TCP 连接一个进程，用完即退 | 常驻进程，长连接复用 |
| 会话绑定 | wrapper 抠 `session` → `--session <file>` | `switch_session {sessionPath}` |
| 读历史 | `get_messages` 全量 + Python 切片 | `get_entries {since}` 增量 |
| workspace | wrapper 抠 `workspace` → `cd` | 读路径无关；写路径每 workspace 一进程 |
| 数据库 | 存 `pi_session_id` | 需补存 `session_file`（阶段 1 映射用） |
