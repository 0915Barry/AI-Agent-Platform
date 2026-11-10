# 问题报告：Pi 原生会话偶发空文件（socat 子进程退出竞态）

> 状态：**已解决**。2026-11-09 完成严格、无重试的 30 轮端到端连续验收。
>
> 关联：`docs/Pi常驻进程与Session加载优化.md`、`docs/microVM预热池设计与实施方案.md`。

---

## 1. 概述（TL;DR）

`cli-smoke-test` 偶发报错：

```
AssertionError: native Session does not contain a user message
```

抓到的现场是：用户数据盘上的 **Pi 原生 session JSONL 文件是 0 字节（空文件）**。

**根因**：Guest 里 `socat` 默认 `-t 0.5`——客户端关闭连接后，socat 只等 0.5 秒就**显式杀掉 EXEC 子进程**（`pi-rpc-session.sh`）。而这个 wrapper 要等待 `pi` 完成 dispose，再执行探针与 `sync`，整个过程可能超过 0.5 秒；wrapper 被杀时退出与持久化收尾尚未完成，数据盘上因而可能只剩一个 0 字节 Session 文件。

- **直接原因**：socat `-t` 超时太短（平台侧，可修）。
- **放大器**：Pi 1.0.0 的 RPC 关闭路径本身也有缺陷（`ace560c3` 上游修复未发版，见 §9 / §10），dispose 偏慢时更容易撞上 0.5 秒。

**修复**：`socat -t 30`（已改 `agent-init.sh` / `prewarm-activate.sh`），在 TCP 连接关闭后给 Pi 和 wrapper 最长 30 秒完成退出、探针与 `sync`。正常情况下子进程退出后 `socat` 会立即结束，并不会固定等待 30 秒。

**验证**：`cli-smoke-test` 已删除“三次重试并继续”和空文件降级逻辑；Session 文件缺失、为空或缺少 `user` / `assistant` 消息都会使当轮立即失败。严格模式下连续 30 轮全部通过（详见 §12）。

---

## 2. 现象与证据

### 2.1 报错表现

smoke test 在停止 microVM、只读挂载数据盘后，读取 Pi 原生 session 文件并断言包含 `user` / `assistant` 消息，偶发（约一半概率，环境慢时更高）断言失败。

### 2.2 关键证据（诊断输出）

失败时打印的原始 session 内容为空：

```text
SESSION_FILE_CONTENT_BEGIN path=.../sessions/--workspace--/2026-11-07T01-27-26-628Z_conversation-xxx.jsonl
SESSION_FILE_CONTENT_END          ← 中间 0 字节，什么都没写
```

成功时的同一文件是完整 6 条记录：

```json
{"type":"session","version":3,"id":"conversation-xxx",...}
{"type":"model_change",...}
{"type":"thinking_level_change",...}
{"type":"message","message":{"role":"system",...}}
{"type":"message","message":{"role":"user","content":[...]}}
{"type":"message","message":{"role":"assistant","content":[...]}}
```

### 2.3 关键判断：全有或全无

空文件是 **0 字节**——连最早写入的 session header（比最终回答早约 6 秒）都没有。

这**曾**被解读为「Pi 一次性整包写盘」。但读过 1.0.0 源码后要修正这个推断：`SessionManager._persist()` 是在**首条 user/assistant 消息**写入时就 `openSync(...,"wx")` 并同步写全部条目，之后逐条 `appendFileSync`（见 §10 E4）——**不是退出时才写**。所以「0 字节」更可能是「文件建了、数据整包没落地」，确切机制由 §10 E1 的探针判定。

---

## 3. 根因分析

### 3.1 会话写盘的真实时机

Pi 的 RPC 模式是「每连接现起现退」：

```text
发消息 → socat 起 pi-rpc-session → 起 pi 进程 → pi 处理 → 回答
       → 关连接 → pi 收到 stdin EOF → pi 进入 shutdown()（§10 E4 的缺陷就在这一段：dispose 可被打断）
       → pi-rpc-session 执行 sync
```

Pi 会在处理会话期间同步创建和追加 Session JSONL；连接关闭后，Pi 还要完成运行时清理，wrapper 随后执行探针与 `sync`，确保本轮写入在 microVM 停止前完成持久化。问题发生在这段**退出与持久化收尾窗口**，不是 suspend/快照阶段才开始保存 Session。

### 3.2 竞态本质

```
TCP 连接关闭
    ├── Pi 正在执行 dispose 并退出
    └── socat 的子进程退出计时器开始计时
            ↓
默认 0.5 秒到期后，socat 显式终止 EXEC 子进程
            ↓
pi-rpc-session.sh 来不及完成退出探针与 sync
```

- wrapper 在超时前正常结束 → Session 内容、探针日志和 `sync` 都能完成。
- wrapper 被 `socat` 提前终止 → Session 可能只留下已创建但内容尚未可靠落盘的 0 字节文件，探针日志也不会生成。

因此，这是 **TCP 断开后 `socat` 终止 EXEC 子进程与 Pi/wrapper 正常退出之间的竞态**。直接触发点位于平台的 Guest RPC 入口，平台可以修复。

### 3.3 为什么早期判断为“平台修不了”

早期只看到“Session 文件存在但为 0 字节”，曾推断为 Pi 内部写入没有发生，因此认为 wrapper 执行 `sync` 也无济于事。

E1 探针随后推翻了这个判断：失败样本不只 Session 为空，连本应由 wrapper 在 Pi 退出后写入的 `session-probe.log` 也完全不存在。这说明 wrapper 根本没有运行到探针和 `sync`，而不是“`sync` 执行了但没有数据可刷”。结合 `socat` 默认 0.5 秒退出超时，最终定位到平台侧可以修复的直接原因。

### 3.4 为什么本平台更容易暴露

| | Pi 正常用法 | 本平台 |
|---|---|---|
| 退出方式 | 用户在终端主动 `/exit` / Ctrl+C | 被 stdin EOF「被动」触发 |
| 进程关系 | Pi 通常直接由终端父进程管理 | Pi 是 `socat EXEC` 启动的子进程 |
| TCP 关闭后 | 通常不存在 `socat -t` 的额外终止计时 | `socat` 默认只给 EXEC 子进程约 0.5 秒退出 |
| 后续动作 | 主机继续运行 | 平台随后停止 microVM，并离线验证数据盘 |

本平台特有的关键条件不是“Pi 一定异步丢写”，而是 **Pi 被放在 `socat EXEC` 子进程链路中，TCP 断开后受到 0.5 秒退出上限约束**。环境变慢时，Pi dispose 与 wrapper 收尾更容易超过该上限，因此故障频率上升。

### 3.5 存储栈：写入 ≠ 刷盘（flake 的精确位置）

很多人把「写盘」混着用，其实是两层不同的动作：

| 动作 | 干什么 | 系统调用 |
|---|---|---|
| **写入（write）** | 应用内存 → **内核页缓存** | `write()` |
| **刷盘（flush/fsync）** | 页缓存 → **物理磁盘** | `sync()` / `fsync()` |

Pi RPC 结束时的完整数据路径：

```text
[Pi 进程内存]  会话对象（JSON）
     │ ① 序列化 + openSync/appendFileSync
     ▼
[Guest 页缓存]  内核内存，write() 的数据先落这
     │ ② sync / fsync（这才是「刷盘」）
     ▼
[virtio-blk → Firecracker]
     ▼
[Host 页缓存]  data.ext4 文件在内核里的缓存
     │ ③ fsync（只有 Writeback 缓存模式才做；默认 Unsafe 不做）
     ▼
[Host 物理磁盘]
```

**早期假设**认为 flake 卡在 ①「写入」，因为文件存在但内容为空。E1 探针后来证明，仅凭 0 字节文件无法得出“Pi 从未写入”的结论：失败时 wrapper 自己的探针日志也缺失，说明整个子进程链路已被提前终止，② 的 `sync` 实际上没有执行。

**修复后的保障**：`socat -t 30` 不负责替 Pi 写 Session，也不固定等待 30 秒；它只是避免在正常清理尚未结束时过早杀死 wrapper。Pi 正常退出后，wrapper 执行探针与 `sync`，随后自行结束，`socat` 立即恢复监听。

**当前完整链路**：Gateway 发 prompt、接收回答并关闭连接 → Pi 收到 stdin EOF 并执行退出清理 → `pi-rpc-session.sh` 等待 Pi 退出、记录探针并执行 `sync` → wrapper 结束 → `socat` 恢复监听。smoke test 随后停止 microVM，并直接读取离线数据盘中的原生 Session，以验证整条链路。

**结论**：「写入数据盘」是整个链路（① 及其后），`sync` 只是其中的刷盘步骤。本次故障不能再表述为“Pi 内部没有写、平台无法修复”，准确结论是：`socat` 在整个退出与刷盘链路完成前终止了 wrapper。

---

## 4. 检测原理（cli-smoke-test 是怎么抓到的）

关键在测试的这几步（`scripts/linux/cli-smoke-test.sh`）：

```text
1. chat 发消息，拿到 Pi 回复
2. conversations 取 conversation_id
3. stop 停止 microVM
4. e2fsck -f -y 数据盘（离线修复 + 回放 ext4 journal）
5. mount -o loop,ro 数据盘（在宿主机上以只读方式挂载）
6. find 出 session 文件 *_<conversation_id>.jsonl
7. 逐行解析 JSONL，筛选 type=="message"，断言 role 含 "user" 和 "assistant"
```

**原理**：测试通过「停止 VM → 宿主机只读挂载数据盘 → 直接读原始文件」来验证「Pi 的会话真的落到了磁盘上、能经得起重启」。它读的是**磁盘上的原始文件**，不是 SQLite 缓存、也不是运行中的 Pi 进程。

正因为读的是「重启后能拿到的那份数据」，所以退出或刷盘链路一旦未完成，测试就能直接抓到缺失、空文件或消息不完整，而不会被 SQLite 历史缓存掩盖。

---

## 5. 影响评估与存储职责

| 层 | 谁写的 | 可靠性 |
|---|---|---|
| 数据盘上的 Pi 原生 Session | Pi 在处理会话期间写入，wrapper 退出前执行 `sync` | Pi 上下文续接的权威来源；修复后严格 30 轮验证通过 |
| SQLite `messages` | Host 根据任务输入和 Pi 返回事件更新 | CLI 历史消息读取缓存，也是独立的可观测记录 |

**结论**：两层数据职责不同，不能互相掩盖。Pi 原生 Session 负责让 Pi 恢复上下文，SQLite 负责平台侧查询和快速展示历史。smoke test 必须分别验证二者：SQLite 内容完整不能替代原生 Session 落盘成功。

**仍需关注的边界**：

1. **更换 Agent Runtime**：其他 Agent 不一定识别 Pi Session 格式，需要 Adapter 或平台消息数据完成迁移。
2. **直接强制终止 Pi/Firecracker**：`socat -t 30` 只保护正常的 TCP 关闭流程，无法保证强制断电前最后一刻尚未持久化的数据。

---

## 6. 触发 / 放大因素

这个竞态一直存在，但**环境越慢越容易输**。观测到的硬证据：同一台嵌套 VM 上，启动实例耗时从早期的约 5.4 秒，涨到后来的 9.5~10 秒（环境负载/热节流/残留进程累积）。

环境变慢 → Pi dispose 与 wrapper 收尾更容易超过 0.5 秒 → `socat` 更容易提前终止子进程 → 空文件更频繁。这解释了「预热池工作前很少遇到、后来频繁遇到」：环境退化放大了原有的退出超时问题。

---

## 7. 修复与验收策略

### 7.1 历史临时方案：smoke test 重试（已删除）

排查早期，`cli-smoke-test.sh` 曾把「chat → 停止 → 读 Session 文件」放入最多三次重试，以降低偶发失败对开发验收的影响。

该方案会掩盖真实回归，现已完全删除。当前单轮只要出现 Session 文件缺失、0 字节、JSONL 无法解析或缺少 `user` / `assistant` 消息，测试立即硬失败，不重试、不降级，也不允许 SQLite 缓存替代原生 Session 检查。

### 7.2 当前修复

Guest RPC 监听的 `socat` 改为 `-t 30`。该参数是**最长退出保护时间**：

- Pi 和 wrapper 正常提前退出时，`socat` 立即结束，不额外等待；
- 收尾较慢时，最多允许 30 秒完成 Pi dispose、探针和 `sync`；
- 超过 30 秒仍未结束时才执行最终终止，防止永久泄漏。

---

## 8. 修复边界

`socat -t 30` 修复的是平台正常通信路径中的过早终止，不等于任何故障下都能保证最后一条消息持久化。

把「被动退出 → 持久化收尾 → 实例停止」拆成三段，逐段看能否加延迟或监测：

| 段 | 位置 | 延迟可行吗 | 监测可行吗 |
|---|---|---|---|
| stdin EOF → Pi 清理 | Pi 进程内部 | ✅ `socat -t 30` 给出正常退出窗口 | ⚠️ 平台只能观察进程与 RPC 事件，不能证明每项内部状态均已提交 |
| Pi 退出 → wrapper 探针与 `sync` | Guest wrapper | ✅ wrapper 等待 Pi 后执行 | ✅ `SESSION_PROBE` 可确认执行路径与文件大小 |
| 强制终止 Firecracker/宿主掉电 | 平台或基础设施 | ❌ 正常退出窗口不再存在 | 只能在重启后执行文件系统和数据完整性检查 |

因此，当前修复可以覆盖正常的 TCP 关闭、实例停止和重启续接，但不能把异常断电转换为事务型存储保证。异常恢复仍需依靠 ext4 journal、`e2fsck`、数据完整性探针和上层任务幂等设计。

---

## 9. 官方文档怎么说（以及它与实测的矛盾）

Pi 官方 RPC 文档（[rpc.md · Shutdown](https://github.com/earendil-works/pi/blob/HEAD/packages/coding-agent/docs/rpc.md)）**main 分支**对关机时序写的是：

> Close the child's stdin to request an orderly shutdown. **Pi disposes the active runtime and waits for pending work to finish before exiting, so give it time to exit before killing it.**

拆成三条可执行的约束：

1. 关闭子进程 stdin → 请求**有序关闭**（orderly shutdown）；
2. **Pi 会「等挂起的工作完成」再退出**；
3. **客户端要「给足时间让它退出，再 kill 它」**。

**但这句话只存在于 main，尚未随任何版本发布。** 我们锁定的 1.0.0 以及到 1.5.0 为止的所有版本，原文都是：

> Close the child's stdin to request an orderly shutdown. **Pi disposes the active runtime before exiting.** Clients should still handle process signals and unexpected exits.

也就是说，**「等挂起工作完成」「给足时间再杀」不是 1.0.0 的行为，而是未发布的修复**。所以不能用这句文档来解释本平台的现状。

但本平台的实测与**main 的行为**仍然矛盾：

- 平台**已经等了**：gateway 的探测连接要等到 socat 重新监听（= `pi` 退出 + `sync` 执行完）才返回，然后才 `stop` VM——**这是「give it time」的严格实现**；
- 可 session 仍然偶发 0 字节。

于是只剩两种可能：

| 假设 | 含义 | 验证结果 |
|---|---|---|
| **A. 1.0.0 没实现「wait for pending work」** | 关闭路径上存在「清理被打断就丢写」的缺陷 | ✅ **成立**，见 §10 |
| **B. session 写没被算作「pending work」** | Pi 确实等了，但等的是别的东西 | ❌ 不成立 |

> 附注：本仓库 `docs/Pi常驻进程与Session加载优化.md` 只记录了「关闭 stdin 即请求有序退出」，遗漏了「give it time before killing it」这句（且该句仅存在于 main），本次一并说明。

---

## 10. 定位实验记录

### E3：版本取证（已完成）

逐 tag 抓取 `docs/rpc.md` 比对 `Shutdown` 一节原文：

| 版本 | 原文 | 有「等挂起工作」 |
|---|---|---|
| 1.0.0（我们锁定）| `Pi disposes the active runtime before exiting.` | ❌ |
| 1.1.0 / 1.2.0 / 1.3.0 / 1.4.0 | 同上 | ❌ |
| 1.5.0（npm 最新，2026-11-02）| 同上 | ❌ |
| main | `...and waits for pending work to finish before exiting, so give it time to exit before killing it.` | ✅ |

结论：**修复只在 main，未发版**；升级到 1.5.0 解决不了。

### E4：源码定位（已完成）

1.0.0 `src/modes/rpc/rpc-mode.ts` 的关闭路径（原文）：

```ts
for (const signal of signals) {
    const handler = () => {
        killTrackedDetachedChildren();
        void shutdown(signal === "SIGHUP" ? 129 : 143, signal);
    };
    process.on(signal, handler);
    signalCleanupHandlers.push(() => process.off(signal, handler)); // 关闭一开始就摘掉
}

async function shutdown(exitCode = 0, signal?: NodeJS.Signals): Promise<never> {
    if (shuttingDown) {
        process.exit(exitCode);          // 第二次调用：立刻退出，dispose 半途而废
    }
    shuttingDown = true;
    for (const cleanup of signalCleanupHandlers) cleanup();  // 摘掉 SIGTERM/SIGHUP
    ...
    await runtimeHost.dispose();         // ← 可被信号 / 第二次调用打断
    ...
    process.exit(exitCode);
}
```

对应的上游修复提交：

| 提交 | 日期 | 标题 |
|---|---|---|
| `ace560c3` | 2026-11-03 | fix(coding-agent): let a started RPC or print shutdown finish; only a repeated signal forces exit (#11752) |
| `b2cb6292` | 2026-11-04 | fix(coding-agent): wait for a started OAuth refresh to be saved before exit (#11812) |

`ace560c3` 的提交说明直接命中本平台场景：

> a second shutdown trigger cut off cleanup.
> - stdin EOF, then SIGTERM: `shutdown()` removed pi's SIGTERM handler when it started, so the signal killed pi (exit 143) mid-`dispose()`.
> - SIGTERM, then stdin EOF: the second `shutdown()` call ran `process.exit()`.

修复后的语义：信号处理器**保留到进程退出**，关闭期间收到一次信号不再视为「跳过清理」，只有**重复信号**才强制退出；`shutdown()` 二次调用改为直接返回。

补充核查：`rpc-mode.ts`、`agent-session-runtime.ts`、`session-manager.ts` 在 1.0.0 与 1.5.0 之间基本一致（后者仅 Windows 路径比较与 fork 写句柄的优化），**所以问题的分界线不是版本号，而是 main 上这两个未发布的修复提交**。

### E1：Guest 侧退出探针（已跑，结果指向 socat）

在 `scripts/guest/pi-rpc-session.sh` 里，`pi` 退出后与 `sync` 后各输出一行：

```
SESSION_PROBE stage=after_pi_exit session=<id> files=<n> max_size=<bytes> pi_exit=<code>
SESSION_PROBE stage=after_sync    session=<id> files=<n> max_size=<bytes> pi_exit=<code>
```

同时追加写入数据盘 `/home/pi/.pi/session-probe.log`（与 Session 文件同盘、同一次 `sync`），`cli-smoke-test.sh` 挂盘时会打印 `SESSION_PROBE_LOG_BEGIN … END`。

**实测结果（修复前的一次失败样本）**：

```text
SESSION_PROBE_LOG_MISSING: Guest 侧未写出探针日志（数据盘上不存在）
SESSION_FILE_CONTENT_BEGIN path=.../conversation-79d945790e254ed0.jsonl
SESSION_FILE_CONTENT_END                ← 0 字节
WARN: Pi native session file is empty (async write flake)  ← 当时尚未改为硬失败
```

关键判读：**同一个 `sync`、同一块盘，session 文件在（0 字节），但探针日志连文件都没有**——说明 wrapper 根本没跑到探针/`sync`，或者被中途杀掉。结合 socat 手册：

> `-t<timeout>`：Default is 0.5 seconds。
> `end-close` 选项说明：when an address of type EXEC … is ended, socat usually will explicitly kill the sub process。

→ 根因坐实为 **socat `-t 0.5` 在 `sync` 前杀掉 wrapper**。修复：`socat -t 30`。

判读表：

| 现象 | 含义 |
|---|---|
| `pi_exit=143`（或 `129`）| Pi 被 SIGTERM/SIGHUP 打断在 dispose 中途 → `ace560c3` 修的缺陷 |
| `pi_exit=0` 且 `max_size=0` | Pi 正常退出却没写任何内容 → 另一类问题 |
| `pi_exit=0` 且 `max_size>0`，但挂盘读到 0 字节 | 写进 Guest 页缓存但没落盘 → 刷盘/宿主侧问题 |
| **探针日志整体缺失** | **wrapper 被 socat 提前杀掉，`sync` 没跑** → 本次实况 |

### E2：不杀 VM 对照（已不必要）

E1 已定位到 socat 杀子进程，E2 的「不杀 VM」对照不再需要。

---

## 11. 关键证据时间线

| 时间点 | cli-smoke-test 结果 |
|---|---|
| Step 3 / lifecycle 轮 | 偶发 `not persisted`，重跑即过（既有 flake，未深究）|
| 抽 socat 循环那次 | 连挂 2 次（当时误判为重构导致，已撤销）|
| 干净基线 / 阶段 0 / 阶段 1 | 通过 |
| 阶段 2（runtime_id 改造）| ❌ → ❌ → ✅ → ❌ 交错随机 |
| 加诊断后 | 抓到了 0 字节空文件 |

---

## 12. 修复方案与结论

根因最终定位为 **socat `-t 0.5` 在 `sync` 前杀掉 wrapper**（§10 E1 实测），不是 Pi 内部缺陷的直接触发。修复已实施并验证：

**✅ 已修**：`socat -t 30`（`agent-init.sh` 的 managed_runtime 监听、`prewarm-activate.sh` 的一次性激活监听），让 wrapper 在 `pi` 退出后有 30 秒完成 dispose + 探针 + `sync`；wrapper 一旦退出，socat 立即结束、外层循环随即恢复监听，不会真的等满 30 秒。

**✅ 已验证**：先完成 7 次连续初步验证；随后删除三次重试与 WARN 降级，从第 1 轮重新计数，完成严格、无重试的 30 轮 `cli-smoke-test`。每轮均执行创建、冷启动、真实模型对话、停止、离线读取 ext4、重启、原生 Session 续接、再次停止、销毁和资源清理。30 轮中的 Session JSONL 均非空，包含 `system`、`user` 和 `assistant` 消息，重启后均能回复首轮唯一标记。

> 排查期间曾引入一个回归：为 `-t 30` 添加的 `#` 注释插在 `\` 续行链中间，把 `runuser -u pi -- env … socat` 劈成两条命令，导致 socat 以 root 运行、wrapper 因「不得以 root 运行」拒绝启动、Pi 从未被拉起（表现为 `Pi RPC connection closed before agent_settled` + session 不存在）。已随本次一并修正（提交 `4345637`）。

**遗留/后续**：

| 事项 | 说明 |
|---|---|
| Pi 1.0.0 关闭路径缺陷（`ace560c3`）| 本次的直接触发是 socat 杀 wrapper，而非该缺陷；但该缺陷仍可能在未来（例如有人直接 SIGTERM）复现，建议 1.6.0 发版后升级 `PI_VERSION` |
| Firecracker `cache_type=Unsafe` | 数据落到宿主页缓存而非物理盘，宿主掉电才可能丢；与本次「挂盘读到空文件」无关，暂不处理 |

当前持续保障措施：

1. 保留 Session 内容诊断与 `SESSION_PROBE` 探针；任何缺失、空文件或内容不完整均立即硬失败，不重试、不降级。
2. 分别验证 Pi 原生 Session 与 SQLite 消息缓存，明确前者用于 Pi 上下文续接、后者用于平台历史查询，禁止缓存掩盖原生 Session 回归。
3. 后续升级 Pi 或替换 Agent Runtime 时，继续把退出协议、会话落盘语义和异常恢复能力纳入兼容性验收。
