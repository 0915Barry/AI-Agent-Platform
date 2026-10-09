import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { AgentConversation, AgentInstance, AgentMessage, AgentTask, AgentTaskEvent, WorkspaceEntry, WorkspaceFile } from "./api";

const statusLabel: Record<string, string> = {
  created: "待启动", starting: "启动中", running: "运行中", stopping: "停止中",
  stopped: "已停止", queued: "排队中", completed: "已完成", failed: "异常",
};

const taskStageLabel: Record<string, string> = {
  queued: "等待 microVM 领取任务",
  started: "任务已领取",
  model_started: "模型正在生成",
  retrying: "模型请求重试中",
  finalizing: "正在整理回答",
  completed: "回答已完成",
  failed: "任务失败",
};

type IconName = "plus" | "play" | "stop" | "trash" | "send" | "server" | "refresh" | "chat";

function Icon({ name }: { name: IconName }) {
  const paths = {
    plus: <path d="M12 5v14M5 12h14" />,
    play: <path d="m8 5 11 7-11 7V5Z" />,
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
    trash: <path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" />,
    send: <path d="m4 4 16 8-16 8 3-8-3-8Zm3 8h13" />,
    server: <path d="M5 4h14v6H5V4Zm0 10h14v6H5v-6ZM8 7h.01M8 17h.01" />,
    refresh: <path d="M20 6v5h-5M4 18v-5h5m9.5-4A7 7 0 0 0 6 7m-.5 8A7 7 0 0 0 18 17" />,
    chat: <path d="M5 5h14v11H9l-4 3V5Z" />,
  };
  return <svg aria-hidden="true" viewBox="0 0 24 24">{paths[name]}</svg>;
}

function formatTime(timestamp?: number) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .format(new Date(timestamp * 1000));
}

export default function App() {
  const [instances, setInstances] = useState<AgentInstance[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<AgentConversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [newId, setNewId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [task, setTask] = useState<AgentTask | null>(null);
  const [taskStage, setTaskStage] = useState("queued");
  const [streamedText, setStreamedText] = useState("");
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [online, setOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const [consoleView, setConsoleView] = useState<"chat" | "files">("chat");
  const [workspacePath, setWorkspacePath] = useState("");
  const [workspaceEntries, setWorkspaceEntries] = useState<WorkspaceEntry[]>([]);
  const [workspacePreview, setWorkspacePreview] = useState<WorkspaceFile | null>(null);
  const idleClocks = useRef<Record<string, { deadline: number; serverIdleSeconds: number }>>({});
  const conversationViewport = useRef<HTMLDivElement | null>(null);

  const selected = useMemo(
    () => instances.find((instance) => instance.id === selectedId) ?? null,
    [instances, selectedId],
  );
  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedConversationId) ?? null,
    [conversations, selectedConversationId],
  );
  const taskActive = task ? ["queued", "running"].includes(task.status) : false;

  const refresh = useCallback(async (quiet = false) => {
    try {
      const [health, nextInstances] = await Promise.all([api.health(), api.listInstances()]);
      const observedAt = Date.now();
      setOnline(health.status === "ok");
      setInstances(nextInstances);
      for (const instance of nextInstances) {
        if (instance.status !== "running" || !instance.runtime) {
          delete idleClocks.current[instance.id];
          continue;
        }
        const candidateDeadline = observedAt +
          Math.max(0, instance.runtime.idleTimeoutSeconds - instance.runtime.idleSeconds) * 1000;
        const previous = idleClocks.current[instance.id];
        const activityWasRefreshed = previous && instance.runtime.idleSeconds < previous.serverIdleSeconds;
        idleClocks.current[instance.id] = {
          deadline: !previous || activityWasRefreshed
            ? candidateDeadline : Math.min(previous.deadline, candidateDeadline),
          serverIdleSeconds: instance.runtime.idleSeconds,
        };
      }
      setSelectedId((current) => current && nextInstances.some((item) => item.id === current)
        ? current : nextInstances[0]?.id ?? null);
      if (!quiet) setError(null);
    } catch (caught) {
      setOnline(false);
      if (!quiet) setError(caught instanceof Error ? caught.message : "无法连接控制面");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadConversations = useCallback(async (instanceId: string) => {
    const next = await api.listConversations(instanceId);
    setConversations(next);
    setSelectedConversationId((current) => current && next.some((item) => item.id === current)
      ? current : next[0]?.id ?? null);
  }, []);

  const loadMessages = useCallback(async (instanceId: string, conversationId: string) => {
    const next = await api.listMessages(instanceId, conversationId);
    setMessages((current) => {
      const unchanged = current.length === next.length && current.every((message, index) => {
        const candidate = next[index];
        return candidate && message.id === candidate.id && message.content === candidate.content;
      });
      return unchanged ? current : next;
    });
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(true), 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setConversations([]);
    setSelectedConversationId(null);
    setMessages([]);
    setTask(null);
    setTaskStage("queued");
    setStreamedText("");
    setActiveTool(null);
    setWorkspacePath("");
    setWorkspaceEntries([]);
    setWorkspacePreview(null);
    if (!selectedId) return;
    void loadConversations(selectedId).catch((caught) =>
      setError(caught instanceof Error ? caught.message : "会话列表加载失败"));
  }, [loadConversations, selectedId]);

  useEffect(() => {
    setMessages([]);
    if (!selectedId || !selectedConversationId) return;
    void loadMessages(selectedId, selectedConversationId).catch((caught) =>
      setError(caught instanceof Error ? caught.message : "消息历史加载失败"));
    const timer = window.setInterval(
      () => void loadMessages(selectedId, selectedConversationId).catch(() => undefined), 3000);
    return () => window.clearInterval(timer);
  }, [loadMessages, selectedConversationId, selectedId]);

  useEffect(() => {
    if (!task || !selectedId || !["queued", "running"].includes(task.status)) return;
    const instanceId = selectedId;
    const taskId = task.id;
    const stream = api.streamTask(instanceId, taskId);
    let finishing = false;
    let terminalReceived = false;
    let receivedText = "";
    let displayedLength = 0;

    const finish = async () => {
      if (finishing) return;
      finishing = true;
      stream.close();
      try {
        const next = await api.getTask(instanceId, taskId);
        setTask(next);
        setTaskStage(next.status);
        await refresh(true);
        if (selectedConversationId) {
          await Promise.all([
            loadMessages(instanceId, selectedConversationId),
            loadConversations(instanceId),
          ]);
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "任务终态加载失败");
      }
    };

    const finishWhenDrained = () => {
      if (terminalReceived && displayedLength >= receivedText.length) void finish();
    };

    // 传输层可能一次送来多个 token。这里把已收到的内容按约 40 帧/秒逐步显示；
    // 积压越多每帧消化越多，既避免整段突现，也不会让长回答在完成后等待太久。
    const paintTimer = window.setInterval(() => {
      const backlog = receivedText.length - displayedLength;
      if (backlog > 0) {
        const step = backlog > 300 ? 8 : backlog > 120 ? 4 : backlog > 40 ? 2 : 1;
        displayedLength = Math.min(receivedText.length, displayedLength + step);
        setStreamedText(receivedText.slice(0, displayedLength));
      }
      finishWhenDrained();
    }, 24);

    const onTaskEvent = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as AgentTaskEvent;
        if (event.type === "queued" || event.type === "started") setTaskStage(event.type);
        if (event.type === "progress" && typeof event.data.stage === "string") {
          setTaskStage(event.data.stage);
        }
        if (event.type === "text" && typeof event.data.delta === "string") {
          receivedText += event.data.delta;
        }
        if (event.type === "tool_call") {
          setActiveTool(typeof event.data.toolName === "string" ? event.data.toolName : "工具");
          setTaskStage("tool_call");
        }
        if (event.type === "tool_result") {
          setActiveTool(null);
          setTaskStage("model_started");
        }
        if (event.type === "completed") {
          terminalReceived = true;
          setTaskStage("finalizing");
          stream.close();
          finishWhenDrained();
        }
        if (event.type === "failed") void finish();
      } catch {
        setError("收到无法解析的任务流事件");
      }
    };

    stream.addEventListener("task-event", onTaskEvent as EventListener);
    stream.onerror = () => {
      // EventSource 会携带 Last-Event-ID 自动重连。若任务其实已经结束，则主动收口。
      void api.getTask(instanceId, taskId).then((next) => {
        if (next.status === "completed") {
          terminalReceived = true;
          setTaskStage("finalizing");
          stream.close();
          finishWhenDrained();
        }
        if (next.status === "failed") void finish();
      }).catch(() => undefined);
    };
    return () => {
      window.clearInterval(paintTimer);
      stream.removeEventListener("task-event", onTaskEvent as EventListener);
      stream.close();
    };
  }, [loadConversations, loadMessages, refresh, selectedConversationId, selectedId, task?.id, task?.status]);

  useEffect(() => {
    const viewport = conversationViewport.current;
    if (!viewport) return;
    const distanceFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    // 只在用户原本靠近底部时跟随流式回答；主动向上阅读历史时不抢滚动位置。
    if (distanceFromBottom < 180) viewport.scrollTop = viewport.scrollHeight;
  }, [messages.length, streamedText.length, task?.status]);

  async function runAction(label: string, operation: () => Promise<unknown>) {
    setAction(label);
    setError(null);
    try {
      await operation();
      await refresh(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败");
    } finally {
      setAction(null);
    }
  }

  function createInstance(event: FormEvent) {
    event.preventDefault();
    const id = newId.trim();
    void runAction("create", async () => {
      const created = await api.createInstance(id || undefined);
      setSelectedId(created.id);
      setNewId("");
    });
  }

  function createConversation() {
    if (!selectedId) return;
    void runAction("conversation", async () => {
      const created = await api.createConversation(selectedId);
      await loadConversations(selectedId);
      setSelectedConversationId(created.id);
      setMessages([]);
      setTask(null);
    });
  }

  function submitMessage(event: FormEvent) {
    event.preventDefault();
    if (!selected || !prompt.trim() || taskActive) return;
    void runAction("task", async () => {
      let conversationId = selectedConversationId;
      if (!conversationId) {
        const created = await api.createConversation(selected.id);
        conversationId = created.id;
        setSelectedConversationId(conversationId);
      }
      const createdTask = await api.createConversationMessage(selected.id, conversationId, prompt.trim());
      setTask(createdTask);
      setTaskStage("queued");
      setStreamedText("");
      setActiveTool(null);
      setPrompt("");
      await Promise.all([loadMessages(selected.id, conversationId), loadConversations(selected.id)]);
    });
  }

  async function loadWorkspace(path = workspacePath) {
    if (!selected || selected.status !== "running") return;
    await runAction("files", async () => {
      const listing = await api.listWorkspace(selected.id, path);
      setWorkspacePath(listing.path);
      setWorkspaceEntries(listing.entries);
      setWorkspacePreview(null);
    });
  }

  async function openWorkspaceEntry(entry: WorkspaceEntry) {
    if (!selected || entry.type === "unsupported") return;
    if (entry.type === "directory") {
      await loadWorkspace(entry.path);
      return;
    }
    await runAction("files", async () => {
      setWorkspacePreview(await api.readWorkspaceFile(selected.id, entry.path));
    });
  }

  function uploadWorkspaceFile(file: File) {
    if (!selected) return;
    if (file.size > 5 * 1024 * 1024) {
      setError("单个文件不能超过 5 MiB");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => void runAction("files", async () => {
      const dataUrl = String(reader.result);
      const contentBase64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
      const target = workspacePath ? `${workspacePath}/${file.name}` : file.name;
      await api.writeWorkspaceFile(selected.id, target, contentBase64);
      const listing = await api.listWorkspace(selected.id, workspacePath);
      setWorkspaceEntries(listing.entries);
    });
    reader.onerror = () => setError("无法读取本地文件");
    reader.readAsDataURL(file);
  }

  function downloadWorkspaceFile(file: WorkspaceFile) {
    const binary = window.atob(file.contentBase64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    const url = URL.createObjectURL(new Blob([bytes]));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.name;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function previewText(file: WorkspaceFile) {
    try {
      const binary = window.atob(file.contentBase64);
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return "该文件不是 UTF-8 文本，请下载后查看。";
    }
  }

  const idleRemaining = useMemo(() => {
    if (!selected?.runtime || selected.status !== "running") return null;
    const deadline = idleClocks.current[selected.id]?.deadline;
    if (!deadline) return Math.max(0, selected.runtime.idleTimeoutSeconds - selected.runtime.idleSeconds);
    return Math.max(0, Math.ceil((deadline - clock) / 1000));
  }, [clock, selected]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark">π</div>
        <div className="brand-copy"><strong>Agent Platform</strong><span>Firecracker control workspace</span></div>
        <div className={`connection ${online ? "online" : "offline"}`}><span className="connection-dot" />{online ? "控制面在线" : "控制面离线"}</div>
      </header>
      <main className="workspace">
        <aside className="instance-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">COMPUTE</span><h1>Agent 实例</h1></div>
            <button className="icon-button" onClick={() => void refresh()} aria-label="刷新实例"><Icon name="refresh" /></button>
          </div>
          <form className="create-form" onSubmit={createInstance}>
            <input value={newId} onChange={(event) => setNewId(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))} placeholder="实例 ID（可选）" maxLength={63} aria-label="新实例 ID" />
            <button disabled={action === "create"} aria-label="创建实例"><Icon name="plus" /></button>
          </form>
          <div className="instance-list">
            {loading ? <div className="list-message">正在读取实例…</div> : null}
            {!loading && instances.length === 0 ? <div className="empty-list"><Icon name="server" /><strong>还没有实例</strong><span>创建一个隔离工作区开始使用</span></div> : null}
            {instances.map((instance) => <button className={`instance-row ${selectedId === instance.id ? "selected" : ""}`} key={instance.id} onClick={() => setSelectedId(instance.id)}>
              <span className={`status-orb ${instance.status}`} /><span className="instance-copy"><strong>{instance.id}</strong><small>{statusLabel[instance.status] ?? instance.status}</small></span><time>{formatTime(instance.updatedAt)}</time>
            </button>)}
          </div>
        </aside>

        <section className="detail-panel">
          {selected ? <>
            <div className="instance-header">
              <div><span className="eyebrow">ACTIVE WORKSPACE</span><h2>{selected.id}</h2></div>
              <div className="actions">
                {selected.status !== "running" ? <button className="primary-button" disabled={action !== null || ["starting", "stopping"].includes(selected.status)} onClick={() => void runAction("start", () => api.startInstance(selected.id))}><Icon name="play" /> {action === "start" ? "正在启动…" : "启动实例"}</button>
                  : <button className="secondary-button" disabled={action !== null} onClick={() => void runAction("stop", () => api.stopInstance(selected.id))}><Icon name="stop" /> {action === "stop" ? "正在停止…" : "停止"}</button>}
                <button className="danger-button" disabled={action !== null} onClick={() => {
                  if (!window.confirm(`销毁 ${selected.id}？数据盘、会话和任务记录将被删除。`)) return;
                  void runAction("destroy", async () => { await api.destroyInstance(selected.id); setSelectedId(null); setSelectedConversationId(null); setMessages([]); setTask(null); });
                }}><Icon name="trash" /> 销毁</button>
              </div>
            </div>
            <div className="metrics">
              <div className="metric-card"><span>实例状态</span><strong className={`metric-status ${selected.status}`}><i /> {statusLabel[selected.status] ?? selected.status}</strong></div>
              <div className="metric-card"><span>进程</span><strong>{selected.runtime?.processAlive ? "运行中" : "未运行"}</strong></div>
              <div className="metric-card"><span>空闲回收</span><strong>{idleRemaining === null ? "—" : `${idleRemaining} 秒`}</strong></div>
              <div className="metric-card"><span>最后更新</span><strong>{formatTime(selected.updatedAt)}</strong></div>
            </div>
            {error ? <div className="error-banner">{error}</div> : null}
            {selected.lastError ? <div className="error-banner">{selected.lastError}</div> : null}
            <div className="agent-console">
              <div className="console-heading"><div><span className="eyebrow">PI AGENT · M15</span><h3>{consoleView === "chat" ? selectedConversation?.title ?? "多轮会话" : "/workspace 文件"}</h3></div><div className="console-tabs"><button className={consoleView === "chat" ? "active" : ""} onClick={() => setConsoleView("chat")}>会话</button><button className={consoleView === "files" ? "active" : ""} onClick={() => { setConsoleView("files"); void loadWorkspace(""); }}>文件</button></div></div>
              {consoleView === "chat" ? <div className="chat-layout">
                <aside className="conversation-sidebar">
                  <button className="new-conversation" onClick={createConversation} disabled={action !== null}><Icon name="plus" /> 新建会话</button>
                  <div className="conversation-list">
                    {conversations.length === 0 ? <div className="conversation-list-empty"><Icon name="chat" /><span>发送第一条消息即可创建会话</span></div> : null}
                    {conversations.map((conversation) => <button key={conversation.id} className={`conversation-row ${selectedConversationId === conversation.id ? "selected" : ""}`} onClick={() => { setSelectedConversationId(conversation.id); setTask(null); setStreamedText(""); setActiveTool(null); }}><strong>{conversation.title}</strong><span>{conversation.preview}</span><time>{formatTime(conversation.updatedAt)}</time></button>)}
                  </div>
                </aside>
                <div className="chat-main">
                  <div className="conversation" ref={conversationViewport}>
                    {messages.length === 0 && !taskActive ? <div className="conversation-empty"><span className="prompt-symbol">›_</span><strong>开始一段持久化对话</strong><p>最近 20 条消息会在固定上下文预算内提供给 Pi Agent；停止或重启实例后历史仍然存在。</p></div> : null}
                    {messages.map((message) => <article className={`message ${message.role === "user" ? "user-message" : "agent-message"}`} key={message.id}><span>{message.role === "user" ? "你" : "Pi Agent"} · {formatTime(message.createdAt)}</span>{message.role === "user" ? <p>{message.content}</p> : <pre>{message.content}</pre>}</article>)}
                    {taskActive ? <article className="message agent-message pending-message"><span>Pi Agent · {activeTool ? `正在使用 ${activeTool}` : taskStageLabel[taskStage] ?? "执行中"} · {task ? Math.max(0, Math.floor(clock / 1000) - task.createdAt) : 0}s</span>{streamedText ? <pre className="streaming-answer">{streamedText}<i className="stream-cursor" /></pre> : <div className="typing"><i /><i /><i /></div>}</article> : null}
                    {task?.status === "failed" ? <div className="task-error">{task.error ?? "任务执行失败"}</div> : null}
                  </div>
                  <form className="prompt-form" onSubmit={submitMessage}>
                    <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder={selected.status === "running" ? "发送消息；Enter 提交，Shift+Enter 换行…" : "请先启动实例"} disabled={selected.status !== "running" || action !== null || taskActive} rows={3} maxLength={16384} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
                    <button className="send-button" disabled={selected.status !== "running" || !prompt.trim() || action !== null || taskActive}><Icon name="send" />{action === "task" ? "发送中" : taskActive ? "执行中" : "发送消息"}</button>
                  </form>
                </div>
              </div> : <div className="file-browser">
                <div className="file-toolbar">
                  <button disabled={!workspacePath || action !== null} onClick={() => void loadWorkspace(workspacePath.split("/").slice(0, -1).join("/"))}>← 返回</button>
                  <code>/workspace{workspacePath ? `/${workspacePath}` : ""}</code>
                  <label className={selected.status !== "running" || action !== null ? "disabled" : ""}>上传文件<input type="file" disabled={selected.status !== "running" || action !== null} onChange={(event) => { const file = event.target.files?.[0]; if (file) uploadWorkspaceFile(file); event.currentTarget.value = ""; }} /></label>
                  <button disabled={selected.status !== "running" || action !== null} onClick={() => { const name = window.prompt("新文件夹名称"); if (!name || !selected) return; const target = workspacePath ? `${workspacePath}/${name}` : name; void runAction("files", async () => { await api.createWorkspaceDirectory(selected.id, target); const listing = await api.listWorkspace(selected.id, workspacePath); setWorkspaceEntries(listing.entries); }); }}>新建文件夹</button>
                  <button disabled={selected.status !== "running" || action !== null} onClick={() => void loadWorkspace()}>刷新</button>
                </div>
                <div className="file-content">
                  <div className="file-list">
                    {selected.status !== "running" ? <div className="file-empty">启动实例后即可管理持久化工作区。</div> : null}
                    {selected.status === "running" && workspaceEntries.length === 0 ? <div className="file-empty">这个目录还是空的，可以上传文件或新建文件夹。</div> : null}
                    {workspaceEntries.map((entry) => <div className="file-row" key={entry.path}><button className="file-name" disabled={entry.type === "unsupported" || action !== null} onClick={() => void openWorkspaceEntry(entry)}><span>{entry.type === "directory" ? "▰" : entry.type === "file" ? "▤" : "?"}</span><strong>{entry.name}</strong></button><small>{entry.type === "file" ? `${entry.size.toLocaleString()} B` : entry.type === "directory" ? "文件夹" : "不支持"}</small><button className="file-delete" disabled={action !== null || entry.type === "unsupported"} onClick={() => { if (!selected || !window.confirm(`删除 ${entry.name}？文件夹必须为空。`)) return; void runAction("files", async () => { await api.deleteWorkspaceEntry(selected.id, entry.path); const listing = await api.listWorkspace(selected.id, workspacePath); setWorkspaceEntries(listing.entries); setWorkspacePreview(null); }); }}>删除</button></div>)}
                  </div>
                  <div className="file-preview">
                    {workspacePreview ? <><div className="preview-heading"><div><strong>{workspacePreview.name}</strong><small>{workspacePreview.size.toLocaleString()} B</small></div><button onClick={() => downloadWorkspaceFile(workspacePreview)}>下载</button></div><pre>{previewText(workspacePreview)}</pre></> : <div className="file-empty">选择文件可预览 UTF-8 文本并下载。</div>}
                  </div>
                </div>
              </div>}
            </div>
          </> : <div className="no-selection"><div className="no-selection-mark">π</div><h2>选择一个 Agent 实例</h2><p>在左侧创建或选择实例，然后启动隔离工作区。</p>{error ? <div className="error-banner">{error}</div> : null}</div>}
        </section>
      </main>
    </div>
  );
}
