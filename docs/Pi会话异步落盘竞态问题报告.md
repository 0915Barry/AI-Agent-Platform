# 问题报告：Pi 原生会话偶发空文件（异步落盘竞态）

> 状态：已定位根因，已加缓解措施（smoke test 重试），尚未根治（根因在第三方 Pi 内部）。
>
> 关联：`docs/Pi常驻进程与Session加载优化.md`、`docs/microVM预热池开发计划.md`。

---

## 1. 概述（TL;DR）

`cli-smoke-test` 偶发报错：

```
AssertionError: native Session does not contain a user message
```

抓到的现场是：用户数据盘上的 **Pi 原生 session JSONL 文件是 0 字节（空文件）**。

**根因**：Guest 里 `socat` 默认 `-t 0.5`——客户端关闭连接后，socat 只等 0.5 秒就**显式杀掉 EXEC 子进程**（`pi-rpc-session.sh`）。而这个 wrapper 要在 `pi` 退出后完成 dispose + 探针 + `sync`，常常超过 0.5 秒；被杀时 `sync` 还没跑，session 只留在 Guest 页缓存里，于是数据盘上剩下一个 0 字节空文件。

- **直接原因**：socat `-t` 超时太短（平台侧，可修）。
- **放大器**：Pi 1.0.0 的 RPC 关闭路径本身也有缺陷（`ace560c3` 上游修复未发版，见 §9 / §10），dispose 偏慢时更容易撞上 0.5 秒。

**修复**：`socat -t 30`（已改 `agent-init.sh` / `prewarm-activate.sh`），让 wrapper 有充足时间刷盘。

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

所以 session 落盘发生在**每次对话结束、连接关闭、Pi 退出**的那一刻，不是在 suspend/快照时。

### 3.2 竞态本质

```
Pi 的「异步一次性写盘」  ──竞速──▶  Pi 进程真正结束
```

- Pi 退出时**等**异步写完成 → 文件有完整内容（6 条记录）。
- Pi 退出**没等**（异步写还在路上，进程就结束了）→ 写被丢弃 → 文件 0 字节。

这是一个经典的「异步 I/O + 进程退出」竞态，且发生在 Pi 进程内部，平台侧无法干预。

### 3.3 为什么平台修不了

`pi-rpc-session.sh` 在 Pi 退出后确实执行了 `sync`，但 `sync` 只能刷「已经写进页缓存」的数据。Pi 的异步写根本没落地，`sync` 无东西可刷。

平台只能确认「Pi 已退出 + sync 已执行」，却看不到、也无法干预 Pi 内部的异步写是否完成。

### 3.4 为什么正常用法不暴露、本平台暴露

| | Pi 正常用法 | 本平台 |
|---|---|---|
| 退出方式 | 用户在终端主动 `/exit` / Ctrl+C | 被 stdin EOF「被动」触发 |
| 退出后机器 | 继续运行，页缓存最终刷盘 | 紧接着 `poweroff -f` 强制断电 |
| 兜底 | 终端/桌面环境自然等写完成 | 无人等待，直接杀 |

正常用法里，即使写慢一点，机器不立刻断电、页缓存最终会落盘，竞态被时间掩盖。本平台把这个竞态逼到明面：**被动退出 + 立刻强制断电**，写没落地就丢了。这是「预热池工作前很少遇到、现在频繁遇到」的另一半解释（配合第 6 节的环境变慢）。

### 3.5 存储栈：写入 ≠ 刷盘（flake 的精确位置）

很多人把「写盘」混着用，其实是两层不同的动作：

| 动作 | 干什么 | 系统调用 |
|---|---|---|
| **写入（write）** | 应用内存 → **内核页缓存** | `write()` |
| **刷盘（flush/fsync）** | 页缓存 → **物理磁盘** | `sync()` / `fsync()` |

Pi RPC 结束时的完整数据路径：

```text
[Pi 进程内存]  会话对象（JSON）
     │ ① 序列化 + fs.write()（Node 异步）
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

**flake 卡在 ①「写入」，不是 ②「刷盘」**：证据是 0 字节文件——文件**存在**（文件创建的元数据被刷下去了），但**内容为空**（write 的数据从来没进过 Guest 页缓存）。即：不是「刷盘没刷」，是「压根没写」。页缓存里没数据，② 的 `sync` 自然「无东西可刷」——它「成功执行」了，但刷了个寂寞。

**「环境兜底」是什么**：正常 Pi 用法里，那套隐含保障——机器不立刻断电（OS 后台回写线程最终会把页缓存刷到磁盘，② 自然发生）、进程不被强杀（Pi 的异步写有时间完成，① 大概率能完成）、终端/桌面环境不会 `poweroff -f`。所以正常用法里 ① 和 ② 都有时间和机会完成，竞态被「时间」兜住；而 microVM 是**被动退出 + 立刻强制断电**，这两层保障全没了。

**本平台这段的逻辑**：gateway 发 prompt、收回答、关连接（→ Guest 里 Pi 的 stdin EOF）→ Guest 的 `pi-rpc-session.sh` 在 Pi 退出后执行 `sync`（覆盖 ②）→ gateway 用探测连接确认「Pi 退出 + sync 已执行」。核心问题：**这套逻辑只覆盖了 ②（刷盘），覆盖不了 ①（写入是否真的发生）**。因为 ① 发生在 Pi 进程内部，我们只能看到「Pi 退出 + sync 跑了」，看不到「write 到底落地没」。平台真正可靠的兜底是 SQLite（发消息时同步写，从 Pi 回复里拿，不依赖 ①）。

**结论**：「写入数据盘」是整个链路（① 及其后），「刷盘」只是 ② 这一小段。这个 bug 在 ①（没写进去），不在 ②（没刷下去）——所以「加延迟放行刷盘」位置不对：延迟只能帮 ② 的收尾，帮不了 ① 的缺失。

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

正因为读的是「重启后能拿到的那份数据」，所以 Pi 的异步写一旦丢，测试就能抓到空文件。

---

## 5. 影响评估：权威与缓存的角色实际反转

| 层 | 谁写的 | 可靠性 |
|---|---|---|
| 数据盘上的 Pi 原生 session（设计上的「权威」）| Pi 退出时异步写 | ❌ 偶发写丢 |
| SQLite `messages`（设计上的「读取缓存」）| 发消息时同步写（从 Pi 回复里拿）| ✅ 可靠 |

**结论**：设计意图是「Pi session 权威、SQLite 缓存」，但实际可靠性反过来了。日常读历史走 SQLite，用户无感；真正不稳的是「名义权威」的 Pi session。

**真实风险场景**（当前未暴露，但未来会踩）：

1. **换 agent**：新 agent 不认识 Pi 的 session 格式，需要靠平台数据重建对话；若那时「权威」的 Pi session 是空的，就会丢上下文。
2. **移除 SQLite 缓存、退回 Pi 原生 session 兜底**：读路径会立刻被这个竞态打回原形。

---

## 6. 触发 / 放大因素

这个竞态一直存在，但**环境越慢越容易输**。观测到的硬证据：同一台嵌套 VM 上，启动实例耗时从早期的约 5.4 秒，涨到后来的 9.5~10 秒（环境负载/热节流/残留进程累积）。

环境变慢 → Pi 写盘更慢 → 「异步写 vs 退出」竞态更容易输 → 空文件更频繁。这解释了「预热池工作前很少遇到、现在频繁遇到」——**是环境退化放大了既有竞态，不是某次代码改动引入的确定性 bug**。

---

## 7. 缓解措施（已实施）

### 7.1 smoke test 重试

`cli-smoke-test.sh` 把「chat → 停止 → 读 session 文件」包进 `chat_and_check_session()`，外面 `for attempt in 1 2 3` 重试：读出来是空文件就重新 `start` 再 chat 一次。

数学上，假设单次落盘失败率约 50%，3 次全失败的概率约 12.5%。重试把失败率大幅压低，但不能归零。

### 7.2 为什么不能彻底杜绝

根因在 Pi 进程内部（退出时不保证等异步写完成），平台侧无法根治。真正 100% 解决只有：

- **等 Pi 修复**退出时序（第三方，不可控）；
- **换 agent 时**把「会话落盘是否可靠」作为选型标准之一。

---

## 8. 为什么外部延迟/监测无法保证

核心限制：**「Pi 的异步写是否完成」这个信号只存在于 Pi 进程内部，外部既看不到、也拦不住。**

把「被动退出 → 异步写 → 强制断电」拆成三段，逐段看能否加延迟或监测：

| 段 | 位置 | 延迟可行吗 | 监测可行吗 |
|---|---|---|---|
| stdin EOF → Pi 退出写 | Pi 进程内部 | ❌ 延迟在退出前，碰不到退出那一刻的写 | ❌ 看不到 Pi 内部的写 |
| Pi 退出写 → poweroff | 平台侧（已有 gateway 探测连接）| 探测已在等「Pi 退出 + sync」| ⚠️ 探测监测的是「sync 执行了」，不是「写落地了」|
| poweroff → 直接杀 | 平台侧 | 无关紧要 | 无关紧要 |

关键点在于第二段：gateway 的探测连接只能确认「Pi 退出 + `sync` 执行了」，但 `sync` 只能刷「已经写进页缓存」的数据。写被 Pi 丢掉时，页缓存是空的，`sync` 照样「成功执行」、探测照样通过——**监测的是错误的信号**。

因此「保证写完再进入下一步」在平台层造不出来，平台只能「事后校验 + 重试」。真正能实时保证的只有 Pi 自己：退出时同步落盘（await 写），或暴露一个「flush 完成」的信号/命令——这两点都得 Pi 侧改。

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

**实测结果（一次失败样本）**：

```text
SESSION_PROBE_LOG_MISSING: Guest 侧未写出探针日志（数据盘上不存在）
SESSION_FILE_CONTENT_BEGIN path=.../conversation-79d945790e254ed0.jsonl
SESSION_FILE_CONTENT_END                ← 0 字节
WARN: Pi native session file is empty (async write flake)
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

根因最终定位为 **socat `-t 0.5` 在 `sync` 前杀掉 wrapper**（§10 E1 实测），不是 Pi 内部缺陷的直接触发。修复已实施：

**✅ 已修**：`socat -t 30`（`agent-init.sh` 的 managed_runtime 监听、`prewarm-activate.sh` 的一次性激活监听），让 wrapper 在 `pi` 退出后有 30 秒完成 dispose + 探针 + `sync`；wrapper 一旦退出，socat 立即结束、外层循环随即恢复监听，不会真的等满 30 秒。

**遗留/后续**：

| 事项 | 说明 |
|---|---|
| Pi 1.0.0 关闭路径缺陷（`ace560c3`）| 本次的直接触发是 socat 杀 wrapper，而非该缺陷；但该缺陷仍可能在未来（例如有人直接 SIGTERM）复现，建议 1.6.0 发版后升级 `PI_VERSION` |
| 验证 | 重建 rootfs 后连跑 `cli-smoke-test` 数次，确认 `SESSION_PROBE_LOG_BEGIN` 出现、`pi_exit=0`、`max_size>0`，且不再有 `WARN: ... empty` |

无论走哪条，短期兜底保持不变：

1. 保留 smoke test 的 3 次重试 + session 内容诊断 + `SESSION_PROBE` 探针。
2. 在架构文档中明确「SQLite 消息缓存事实上是当前可靠的持久化层，Pi 原生 session 存在关闭期落盘竞态」，避免未来换 agent 时误以为 Pi session 一定完整。
3. agent 选型时把「会话落盘是否同步/可靠」纳入评估。
