# 控制面 HTTP API 契约

> 面向对象：上层 / 中层的前后端开发。
> 本文档描述「AI Agent 平台」下层控制面（Control Plane）对外暴露的全部 HTTP / SSE 接口。
> 所有字段名、状态值、事件类型均以 `services/control-plane/server.py` 与 `task_store.py` 的实际实现为准。

---

## 0. 对接前提与联调方式

### 0.1 服务地址与监听范围

- 控制面只监听 **`http://127.0.0.1:18090`**（loopback，代码层强制，不对外监听）。
- 实际部署时**认证是开启的**（`--require-auth`），工作区 / 会话接口必须登录后才能调用。
- 全程 **HTTP 明文**，没有 TLS。

### 0.2 推荐联调拓扑（同局域网）

下层负责人负责把 18090 端口「搬」到前端开发机的 `127.0.0.1`，前端 dev server 再做同源代理，从而**完全避开 CORS 和跨域 Cookie 问题**：

```text
浏览器 ──► 前端 dev server (localhost:5173)
                │  proxy /api → http://127.0.0.1:18090
                ▼
        SSH 隧道 ──► 下层控制面 (127.0.0.1:18090)
```

- 下层在 macOS 上先跑 `./run.sh tunnel`（把 Ubuntu 里的 18090 带到本机 127.0.0.1）。
- 再把它反向转发给前端开发机（假设其可 SSH 登录）：

  ```bash
  ssh -R 18090:127.0.0.1:18090 前端同事@她的机器IP
  ```

- 前端 dev server 配代理（Vite 示例）：

  ```js
  // vite.config.js
  export default {
    server: {
      proxy: {
        '/api': { target: 'http://127.0.0.1:18090', changeOrigin: false },
      },
    },
  }
  ```

> 走这条同源代理链路后，Cookie 的 `SameSite=Strict` 依然生效（浏览器始终只跟 `localhost` 同源通信），
> SSE 也不需要额外 CORS 头。

---

## 1. 通用约定

### 1.1 请求 / 响应

- 所有接口（除 SSE）均使用 **JSON**；请求体 `Content-Type: application/json`。
- 成功响应的 HTTP 状态码：`200`（查询/动作）、`201`（创建）、`202`（已受理，异步任务）。
- 字段命名统一 **camelCase**；时间戳统一为 **Unix 秒**（整数）。

### 1.2 错误格式

所有可预期错误返回统一结构：

```json
{ "error": { "code": "instance_not_found", "message": "instance not found: xxx" } }
```

`code` 是稳定的机器可读错误码（见 §10），前端应据 `code` 分支处理，而不是解析 `message`。

### 1.3 认证方式

- 登录后服务端下发 **HttpOnly Cookie**（名称 `aap_session`，`Path=/; HttpOnly; SameSite=Strict`）。
- 前端使用 `fetch(..., { credentials: 'include' })` 让浏览器自动携带 Cookie。
- 未登录访问受保护接口返回 `401 authentication_required`。

---

## 2. 认证接口

### `GET /api/auth/session`
查询当前登录状态（无需 Cookie）。

```json
// 200
{ "authenticated": true, "user": { "id": "user-xxxxxxxxxxxxxxxx", "username": "alice" } }
// 未登录：authenticated=false, user=null
```

### `POST /api/auth/login`
登录。

- 请求体：`{ "username": "alice", "password": "****" }`
- 响应 `200`，并在 `Set-Cookie` 中下发会话 Cookie：

```json
{ "user": { "id": "user-xxxxxxxxxxxxxxxx", "username": "alice" }, "expiresAt": 1750000000 }
```

- 错误：`401 invalid_credentials`（用户名或密码错误）、`400 invalid_credentials`（字段类型不对）。

### `POST /api/auth/logout`
登出。响应 `200 { "status": "logged_out" }`，并清除 Cookie。

---

## 3. 工作区接口

工作区是用户的逻辑分组，一个用户可拥有多个工作区，每个用户始终有一个「当前工作区」。

### `GET /api/workspaces`
列出当前用户全部工作区。

```json
{
  "workspaces": [
    {
      "id": "workspace-xxxxxxxxxxxxxxxx",
      "name": "默认工作区",
      "directoryName": "workspace-xxxxxxxxxxxxxxxx",
      "createdAt": 1750000000,
      "updatedAt": 1750000000,
      "current": true,
      "conversationCount": 3
    }
  ]
}
```

> 未登录（认证关闭时）访问会返回 `404 route_not_found`。

### `POST /api/workspaces`
创建工作区。请求体 `{ "name": "我的项目" }` → `201` 返回工作区对象（不带 `conversationCount`）。

- 名称去首尾空格、折叠内部空白，≤120 UTF-8 字节。
- 重名返回 `409 workspace_invalid`。

### `POST /api/workspaces/{workspace_id}/select`
切换当前工作区。`200` 返回该工作区对象（`current: true`）。

### `DELETE /api/workspaces/{workspace_id}`
删除工作区（会先停止计算资源，再删会话与用户盘目录，需二次确认由前端负责）。

```json
{
  "id": "workspace-xxxxxxxxxxxxxxxx",
  "status": "deleted",
  "deletedConversations": 3,
  "currentWorkspace": { "id": "...", "name": "..." }
}
```

- 有进行中任务时：`409 workspace_busy`。

---

## 4. 会话接口（工作区维度）

### `GET /api/workspaces/{workspace_id}/conversations`
列出工作区下的会话（按最近活动倒序）。

```json
{
  "conversations": [
    {
      "id": "conversation-xxxxxxxxxxxxxxxx",
      "instanceId": "agent-xxxxxxxxxxxx",
      "workspaceId": "workspace-xxxxxxxxxxxxxxxx",
      "piSessionId": "conversation-xxxxxxxxxxxxxxxx",
      "sessionBackend": "pi-native",
      "title": "帮我看看这段代码",
      "preview": "最新一条消息内容",
      "messageCount": 12,
      "createdAt": 1750000000,
      "updatedAt": 1750000001
    }
  ]
}
```

### `POST /api/workspaces/{workspace_id}/conversations`
新建会话。`201` 返回会话对象。新会话 `sessionBackend` 为 `pi-native`（Pi 原生 JSONL Session 持久化）。

### `DELETE /api/workspaces/{workspace_id}/conversations/{conversation_id}`
删除会话（会先停止当前实例、再清理数据盘 Pi Session 与数据库元数据）。

```json
{ "id": "conversation-xxxxxxxxxxxxxxxx", "status": "deleted" }
```

- 会话仍有排队/执行中任务：`409 conversation_busy`。

---

## 5. 聊天（发消息 + SSE 流式 + 历史）

> 上层「聊天」功能只需这三个接口：发消息、SSE 订阅、读历史。实例由下层自动拉起，上层无需关心。

### 5.1 发消息

`POST /api/instances/{instance_id}/conversations/{conversation_id}/messages`

- 请求体：`{ "content": "帮我把 utils.py 的 format 改成异步" }`（≤16 KiB）
- 响应 `202`，返回一个**任务对象**，其中 `id` 是本次任务的唯一标识（用于订阅 SSE）：

```json
{
  "id": "task-xxxxxxxxxxxxxxxx",
  "instanceId": "agent-xxxxxxxxxxxx",
  "prompt": "帮我把 utils.py 的 format 改成异步",
  "status": "queued",
  "output": null,
  "error": null,
  "createdAt": 1750000000,
  "updatedAt": 1750000000,
  "conversationId": "conversation-xxxxxxxxxxxxxxxx",
  "workspaceId": "workspace-xxxxxxxxxxxxxxxx",
  "piSessionId": "conversation-xxxxxxxxxxxxxxxx",
  "sessionBackend": "pi-native"
}
```

> `instance_id` 从哪里来？工作区列表 / 会话列表的返回里没有直接给用户实例 ID。
> 上层可通过任意会话对象里的 `instanceId` 拿到；或在无会话时先建会话再取 `instanceId`。
> （更友好的做法见 §12 待办：给工作区列表直接返回其 `instanceId`。）

### 5.2 订阅流式结果（SSE）

`GET /api/instances/{instance_id}/tasks/{task_id}/stream`

- 返回 `text/event-stream`，逐条推送任务事件，遇到 `run.completed` / `run.failed` 后**服务端主动关闭连接**。
- 支持断线续传：重连时带上 `Last-Event-ID: <sequence>`（或 `?after=<sequence>`），服务端只补发遗漏事件。

帧格式：

```text
id: 12
event: task-event
data: {"sequence":12,"type":"message.delta","data":{"delta":"改好"},"createdAt":1750000000}

: heartbeat
```

- `id` 是事件序号（游标）；`event` 恒为 `task-event`；`data` 是完整事件 JSON。
- 空闲时每 10 秒发一条注释行 `: heartbeat` 保活。

### 5.3 读历史（分页）

`GET /api/instances/{instance_id}/conversations/{conversation_id}/messages?limit=50&before=<sequence>`

```json
{
  "messages": [
    {
      "sequence": 1,
      "id": "pi-conversation-xxx-1",
      "conversationId": "conversation-xxxxxxxxxxxxxxxx",
      "role": "user",
      "content": "帮我看看这段代码",
      "taskId": null,
      "createdAt": 1750000000
    }
  ],
  "nextBefore": null,
  "startupTiming": null,
  "historyTiming": null
}
```

- `limit`：1–100，默认 50；`before`：传上一条 `nextBefore` 向前翻页（传 `null` 表示取最新一页）。
- `nextBefore` 非 `null` 表示还有更早消息，取下一页时把它作为 `before`。
- 读历史可能触发冷启动，`startupTiming` / `historyTiming` 是下层性能诊断字段，上层可忽略。

### 5.4 一个完整聊天时序

```text
① POST /api/auth/login                 → 拿到 Cookie（浏览器自动保存）
② GET  /api/workspaces                 → 选工作区（或 POST 新建）
③ POST /api/workspaces/{wid}/conversations  → 建会话，拿到 conversationId + instanceId
④ POST /api/instances/{iid}/conversations/{cid}/messages   → 202，拿到 taskId
⑤ GET  /api/instances/{iid}/tasks/{tid}/stream               → SSE 逐字收结果
⑥ （下次打开）GET /api/instances/{iid}/conversations/{cid}/messages → 读历史
```

---

## 6. 任务接口（底层直连 prompt，上层一般不用）

直接对运行实例下发一个 prompt，不挂会话。

- `POST /api/instances/{instance_id}/tasks`，请求体 `{ "prompt": "..." }` → `202` 任务对象。
- `GET  /api/instances/{instance_id}/tasks/{task_id}` → 任务对象（含 `status` / `output` / `error`）。
- `GET  /api/instances/{instance_id}/tasks/{task_id}/events?after=0` → `{ "events": [...] }`（一次性拉事件，轮询用）。
- `GET  /api/instances/{instance_id}/tasks/{task_id}/stream` → SSE（同 §5.2）。

---

## 7. 实例管理接口（下层诊断用，上层通常不直接调）

> 上层「发消息」会自动按需启动实例，**不需要**主动管理实例。这里列出仅供参考。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/instances` | 创建实例，body `{ "id": "可选" }` → 201 |
| GET | `/api/instances` | 列出实例 → `{ "instances": [...] }` |
| GET | `/api/instances/{id}` | 查询单个实例 |
| POST | `/api/instances/{id}/start` | 启动 |
| POST | `/api/instances/{id}/heartbeat` | 心跳（刷新活动时间） |
| POST | `/api/instances/{id}/stop` | 停止 |
| POST | `/api/instances/{id}/suspend` | 暂停并创建快照 |
| POST | `/api/instances/{id}/resume` | 从快照恢复 |
| DELETE | `/api/instances/{id}` | 销毁 |

实例状态机：`created → starting → running → stopping → stopped`；另有 `suspending / suspended / resuming` 与 `failed`。

实例对象：

```json
{
  "id": "agent-xxxxxxxxxxxx",
  "status": "running",
  "createdAt": 1750000000,
  "updatedAt": 1750000001,
  "lastError": null,
  "ownerId": "user-xxxxxxxxxxxxxxxx",
  "runtime": {
    "hostIp": "172.29.0.1", "guestIp": "172.29.0.2", "rpcPort": 8080,
    "idleSeconds": 12, "busy": false, "processAlive": true,
    "startupTiming": { "outcome": "ready", "totalMs": 5000, "host": {}, "guest": {} }
  }
}
```

`runtime` 只在实例处于运行类状态时出现，且字段偏底层，上层不要依赖其细节。

---

## 8. SSE 事件类型与 payload

| `type` | `data`（payload） | 含义 |
|---|---|---|
| `queued` | `{}` | 任务已入队 |
| `run.started` | `{}` | 下层消费者已领取，开始执行 |
| `startup_timing` | `{ outcome, totalMs, host:{...}, guest:{...} }` | （冷启动时的）启动耗时诊断，可忽略 |
| `progress` | `{ "stage": "model_started" }` | 开始调模型 |
| `progress` | `{ "stage": "retrying", "attempt": 1, "delayMs": 800 }` | 自动重试 |
| `message.delta` | `{ "delta": "改好" }` | 文本增量，**前端直接拼接展示** |
| `tool.started` | `{ "toolCallId": "...", "toolName": "read", "arguments": {...} }` | Agent 开始调用工具 |
| `tool.completed` | `{ "toolCallId": "...", "toolName": "read", "isError": false, "preview": "...", "truncated": false }` | 工具返回（`preview` 截断至 2000 字符） |
| `run.completed` | `{ "output": "最终完整回复文本" }` | 任务真正结束（收到即关闭连接） |
| `run.failed` | `{ "error": "错误信息" }` | 任务失败（收到即关闭连接） |

前端渲染要点：

- 把每个 `message.delta` 事件的 `delta` 依次追加到当前回答即可（流式打字效果）。
- `tool.started` / `tool.completed` 可作为「思考/工具执行过程」折叠展示。
- `run.completed` 的 `output` 是最终完整文本，可用于兜底覆盖拼接结果。

---

## 9. 数据结构字段速查

### 9.1 工作区 workspace

`id` `name` `directoryName` `createdAt` `updatedAt` `current`（列表含 `conversationCount`）

### 9.2 会话 conversation

`id` `instanceId` `workspaceId` `piSessionId` `sessionBackend` `title` `preview` `messageCount` `createdAt` `updatedAt`

### 9.3 消息 message

`sequence` `id` `conversationId` `role`（`user` / `assistant`） `content` `taskId` `createdAt`

### 9.4 任务 task

`id` `instanceId` `prompt` `status`（`queued` / `running` / `completed` / `failed`） `output` `error` `createdAt` `updatedAt` `conversationId` `workspaceId` `piSessionId` `sessionBackend`

### 9.5 事件 event

`sequence` `type` `data` `createdAt`

### 9.6 用户 user

`id` `username`

---

## 10. 主要错误码

| HTTP | code | 场景 |
|---|---|---|
| 400 | `invalid_json` / `invalid_request` / `invalid_content_length` | 请求体解析失败 |
| 400 | `invalid_prompt` / `invalid_message` / `invalid_conversation_title` | 字段为空或类型错 |
| 401 | `authentication_required` | 未登录 |
| 401 | `invalid_credentials` | 用户名或密码错误 |
| 404 | `instance_not_found` / `task_not_found` / `conversation_not_found` / `workspace_not_found` | 资源不存在或无权访问 |
| 404 | `route_not_found` | 路由不存在（认证关闭时工作区接口也会返回此码） |
| 409 | `instance_exists` / `instance_quota_exceeded` / `running_quota_exceeded` | 创建/运行配额（每用户限 1 实例、1 运行） |
| 409 | `instance_busy` / `instance_running` / `instance_not_running` / `instance_suspended` / `instance_not_suspended` | 实例状态冲突 |
| 409 | `workspace_invalid` / `workspace_busy` / `conversation_busy` | 名称冲突 / 有进行中任务 |
| 413 | `prompt_too_large` / `message_too_large` / `conversation_title_too_large` / `request_too_large` | 内容超限 |
| 500 | `instance_*_failed` / `task_create_failed` / `workspace_cleanup_failed` / `conversation_cleanup_failed` | 下层执行失败 |
| 502 | `session_read_failed` | 读 Pi 原生会话失败 |

---

## 11. 前端联调最小示例

```js
// 1. 登录（credentials: include 让浏览器存 Cookie）
await fetch('/api/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  credentials: 'include',
  body: JSON.stringify({ username: 'alice', password: '******' }),
});

// 2. 取工作区
const { workspaces } = await (await fetch('/api/workspaces', { credentials: 'include' })).json();
const wid = workspaces.find(w => w.current).id;

// 3. 建会话
const conv = await (await fetch(`/api/workspaces/${wid}/conversations`, {
  method: 'POST', credentials: 'include',
})).json();

// 4. 发消息，拿到 taskId
const task = await (await fetch(
  `/api/instances/${conv.instanceId}/conversations/${conv.id}/messages`,
  { method: 'POST', headers: { 'Content-Type': 'application/json' },
    credentials: 'include', body: JSON.stringify({ content: '你好' }) },
)).json();

// 5. 订阅 SSE
const es = new EventSource(
  `/api/instances/${conv.instanceId}/tasks/${task.id}/stream`
);
es.addEventListener('task-event', (e) => {
  const ev = JSON.parse(e.data);
  if (ev.type === 'message.delta') appendText(ev.data.delta);
  else if (ev.type === 'run.completed') console.log('完成:', ev.data.output);
  else if (ev.type === 'run.failed') console.error(ev.data.error);
});
```

> `EventSource` 会自动处理 `Last-Event-ID` 断线续传，但默认不支持 Cookie（走同源代理时同一站点可带上）；跨源时建议改用 `fetch` + 手动解析 SSE，或前端自行处理 `Authorization`（当前后端只认 Cookie，暂不支持 Bearer）。

---

## 12. 已识别但尚未落地的对接缺口（给下层负责人）

1. **工作区列表没有直接返回用户实例 `instanceId`**，上层发消息前要绕一道「建会话→取 instanceId」。建议给 `GET /api/workspaces` 直接附带 `instanceId`。
2. **无 CORS 头**：当前只支持同源代理对接，若上层坚持跨源直连，需在控制面加 `Access-Control-Allow-Origin` + `Access-Control-Allow-Credentials` 并处理 OPTIONS 预检（含 SSE）。
3. **只认 Cookie，不支持 Bearer Token**：SSE 跨源时 Cookie 会受 `SameSite=Strict` 限制，若要走跨源，需扩展认证方式。
4. **明文 HTTP**：生产对接前需加 TLS（反向代理终止）并给 Cookie 加 `Secure`。
