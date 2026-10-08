import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { AgentInstance, AgentTask } from "./api";

const statusLabel: Record<string, string> = {
  created: "待启动",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  stopped: "已停止",
  queued: "排队中",
  completed: "已完成",
  failed: "异常",
};

function Icon({ name }: { name: "plus" | "play" | "stop" | "trash" | "send" | "server" | "refresh" }) {
  const paths = {
    plus: <path d="M12 5v14M5 12h14" />,
    play: <path d="m8 5 11 7-11 7V5Z" />,
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
    trash: <path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" />,
    send: <path d="m4 4 16 8-16 8 3-8-3-8Zm3 8h13" />,
    server: <path d="M5 4h14v6H5V4Zm0 10h14v6H5v-6ZM8 7h.01M8 17h.01" />,
    refresh: <path d="M20 6v5h-5M4 18v-5h5m9.5-4A7 7 0 0 0 6 7m-.5 8A7 7 0 0 0 18 17" />,
  };
  return <svg aria-hidden="true" viewBox="0 0 24 24">{paths[name]}</svg>;
}

function formatTime(timestamp?: number) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(
    new Date(timestamp * 1000),
  );
}

export default function App() {
  const [instances, setInstances] = useState<AgentInstance[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [newId, setNewId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [task, setTask] = useState<AgentTask | null>(null);
  const [online, setOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [clock, setClock] = useState(() => Date.now());
  const idleClocks = useRef<Record<string, { deadline: number; serverIdleSeconds: number }>>({});

  const selected = useMemo(
    () => instances.find((instance) => instance.id === selectedId) ?? null,
    [instances, selectedId],
  );

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
        const candidateDeadline =
          observedAt +
          Math.max(0, instance.runtime.idleTimeoutSeconds - instance.runtime.idleSeconds) * 1000;
        const previous = idleClocks.current[instance.id];
        const activityWasRefreshed = previous && instance.runtime.idleSeconds < previous.serverIdleSeconds;
        idleClocks.current[instance.id] = {
          // 服务端秒数取整会产生约一秒误差。没有新活动时截止时间只能提前，
          // 不能因下一次轮询重新向后移动，从而避免页面出现 298 → 299。
          deadline:
            !previous || activityWasRefreshed
              ? candidateDeadline
              : Math.min(previous.deadline, candidateDeadline),
          serverIdleSeconds: instance.runtime.idleSeconds,
        };
      }
      setSelectedId((current) =>
        current && nextInstances.some((instance) => instance.id === current)
          ? current
          : nextInstances[0]?.id ?? null,
      );
      if (!quiet) setError(null);
    } catch (caught) {
      setOnline(false);
      if (!quiet) setError(caught instanceof Error ? caught.message : "无法连接控制面");
    } finally {
      setLoading(false);
    }
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
    if (!task || !selectedId || !["queued", "running"].includes(task.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.getTask(selectedId, task.id);
        setTask(next);
        if (next.status === "completed" || next.status === "failed") void refresh(true);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "任务状态刷新失败");
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [refresh, selectedId, task]);

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
      setTask(null);
    });
  }

  function submitTask(event: FormEvent) {
    event.preventDefault();
    if (!selected || !prompt.trim()) return;
    void runAction("task", async () => {
      const created = await api.createTask(selected.id, prompt.trim());
      setTask(created);
      setPrompt("");
    });
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
        <div className="brand-copy">
          <strong>Agent Platform</strong>
          <span>Firecracker control workspace</span>
        </div>
        <div className={`connection ${online ? "online" : "offline"}`}>
          <span className="connection-dot" />
          {online ? "控制面在线" : "控制面离线"}
        </div>
      </header>

      <main className="workspace">
        <aside className="instance-panel">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">COMPUTE</span>
              <h1>Agent 实例</h1>
            </div>
            <button className="icon-button" onClick={() => void refresh()} aria-label="刷新实例">
              <Icon name="refresh" />
            </button>
          </div>

          <form className="create-form" onSubmit={createInstance}>
            <input
              value={newId}
              onChange={(event) => setNewId(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
              placeholder="实例 ID（可选）"
              maxLength={63}
              aria-label="新实例 ID"
            />
            <button disabled={action === "create"} aria-label="创建实例">
              <Icon name="plus" />
            </button>
          </form>

          <div className="instance-list">
            {loading ? <div className="list-message">正在读取实例…</div> : null}
            {!loading && instances.length === 0 ? (
              <div className="empty-list">
                <Icon name="server" />
                <strong>还没有实例</strong>
                <span>创建一个隔离工作区开始使用</span>
              </div>
            ) : null}
            {instances.map((instance) => (
              <button
                className={`instance-row ${selectedId === instance.id ? "selected" : ""}`}
                key={instance.id}
                onClick={() => {
                  setSelectedId(instance.id);
                  setTask(null);
                }}
              >
                <span className={`status-orb ${instance.status}`} />
                <span className="instance-copy">
                  <strong>{instance.id}</strong>
                  <small>{statusLabel[instance.status] ?? instance.status}</small>
                </span>
                <time>{formatTime(instance.updatedAt)}</time>
              </button>
            ))}
          </div>
        </aside>

        <section className="detail-panel">
          {selected ? (
            <>
              <div className="instance-header">
                <div>
                  <span className="eyebrow">ACTIVE WORKSPACE</span>
                  <h2>{selected.id}</h2>
                </div>
                <div className="actions">
                  {selected.status !== "running" ? (
                    <button
                      className="primary-button"
                      disabled={action !== null || ["starting", "stopping"].includes(selected.status)}
                      onClick={() => void runAction("start", () => api.startInstance(selected.id))}
                    >
                      <Icon name="play" /> {action === "start" ? "正在启动…" : "启动实例"}
                    </button>
                  ) : (
                    <button
                      className="secondary-button"
                      disabled={action !== null}
                      onClick={() => void runAction("stop", () => api.stopInstance(selected.id))}
                    >
                      <Icon name="stop" /> {action === "stop" ? "正在停止…" : "停止"}
                    </button>
                  )}
                  <button
                    className="danger-button"
                    disabled={action !== null}
                    onClick={() => {
                      if (!window.confirm(`销毁 ${selected.id}？数据盘和任务记录将被删除。`)) return;
                      void runAction("destroy", async () => {
                        await api.destroyInstance(selected.id);
                        setSelectedId(null);
                        setTask(null);
                      });
                    }}
                  >
                    <Icon name="trash" /> 销毁
                  </button>
                </div>
              </div>

              <div className="metrics">
                <div className="metric-card">
                  <span>实例状态</span>
                  <strong className={`metric-status ${selected.status}`}>
                    <i /> {statusLabel[selected.status] ?? selected.status}
                  </strong>
                </div>
                <div className="metric-card">
                  <span>进程</span>
                  <strong>{selected.runtime?.processAlive ? "运行中" : "未运行"}</strong>
                </div>
                <div className="metric-card">
                  <span>空闲回收</span>
                  <strong>{idleRemaining === null ? "—" : `${idleRemaining} 秒`}</strong>
                </div>
                <div className="metric-card">
                  <span>最后更新</span>
                  <strong>{formatTime(selected.updatedAt)}</strong>
                </div>
              </div>

              {error ? <div className="error-banner">{error}</div> : null}
              {selected.lastError ? <div className="error-banner">{selected.lastError}</div> : null}

              <div className="agent-console">
                <div className="console-heading">
                  <div>
                    <span className="eyebrow">PI AGENT</span>
                    <h3>任务控制台</h3>
                  </div>
                  <span className={`task-chip ${task?.status ?? "idle"}`}>
                    {task ? statusLabel[task.status] ?? task.status : "等待任务"}
                  </span>
                </div>

                <div className="conversation">
                  {!task ? (
                    <div className="conversation-empty">
                      <span className="prompt-symbol">›_</span>
                      <strong>向隔离 Agent 下发任务</strong>
                      <p>任务在 microVM 中执行，模型密钥始终留在宿主 Gateway。</p>
                    </div>
                  ) : (
                    <>
                      <article className="message user-message">
                        <span>你的任务</span>
                        <p>{task.prompt}</p>
                      </article>
                      <article className="message agent-message">
                        <span>Pi Agent · {statusLabel[task.status] ?? task.status}</span>
                        {task.status === "queued" || task.status === "running" ? (
                          <div className="typing"><i /><i /><i /></div>
                        ) : (
                          <pre>{task.output ?? task.error ?? "任务没有返回内容"}</pre>
                        )}
                      </article>
                    </>
                  )}
                </div>

                <form className="prompt-form" onSubmit={submitTask}>
                  <textarea
                    value={prompt}
                    onChange={(event) => setPrompt(event.target.value)}
                    placeholder={selected.status === "running" ? "描述你希望 Agent 完成的任务…" : "请先启动实例"}
                    disabled={selected.status !== "running" || action !== null}
                    rows={3}
                    maxLength={32768}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        event.currentTarget.form?.requestSubmit();
                      }
                    }}
                  />
                  <button
                    className="send-button"
                    disabled={selected.status !== "running" || !prompt.trim() || action !== null}
                  >
                    <Icon name="send" />
                    {action === "task" ? "发送中" : "发送任务"}
                  </button>
                </form>
              </div>
            </>
          ) : (
            <div className="no-selection">
              <div className="no-selection-mark">π</div>
              <h2>选择一个 Agent 实例</h2>
              <p>在左侧创建或选择实例，然后启动隔离工作区。</p>
              {error ? <div className="error-banner">{error}</div> : null}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
