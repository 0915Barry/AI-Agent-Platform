export type InstanceStatus = "created" | "starting" | "running" | "stopping" | "stopped" | "failed";
export type TaskStatus = "queued" | "running" | "completed" | "failed";

export interface AuthUser {
  id: string;
  username: string;
}

export class ApiRequestError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export interface RuntimeStatus {
  processAlive: boolean;
  idleSeconds: number;
  idleTimeoutSeconds: number;
  busy?: boolean;
}

export interface AgentInstance {
  id: string;
  status: InstanceStatus;
  createdAt: number;
  updatedAt: number;
  lastError: string | null;
  agentName: string;
  systemPrompt: string;
  toolMode: "read_only" | "read_write";
  runtime?: RuntimeStatus;
}

export interface AgentTask {
  id: string;
  instanceId: string;
  prompt: string;
  status: TaskStatus;
  output: string | null;
  error: string | null;
  createdAt: number;
  updatedAt: number;
  conversationId: string | null;
}

export interface AgentTaskEvent {
  sequence: number;
  type: "queued" | "started" | "progress" | "text" | "tool_call" | "tool_result" | "completed" | "failed";
  data: Record<string, unknown>;
  createdAt: number;
}

export interface AgentConversation {
  id: string;
  instanceId: string;
  title: string;
  preview: string;
  createdAt: number;
  updatedAt: number;
}

export interface AgentMessage {
  id: string;
  conversationId: string;
  role: "user" | "assistant";
  content: string;
  taskId: string | null;
  createdAt: number;
}

export interface WorkspaceEntry {
  name: string;
  path: string;
  type: "file" | "directory" | "unsupported";
  size: number;
  modifiedAt: number;
}

export interface WorkspaceListing {
  path: string;
  entries: WorkspaceEntry[];
}

export interface WorkspaceFile {
  path: string;
  name: string;
  size: number;
  contentBase64: string;
}

interface ApiErrorPayload {
  error?: { code?: string; message?: string };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const payload = (await response.json().catch(() => ({}))) as T & ApiErrorPayload;
  if (!response.ok) {
    throw new ApiRequestError(
      response.status,
      payload.error?.code ?? "request_failed",
      payload.error?.message ?? `请求失败（HTTP ${response.status}）`,
    );
  }
  return payload;
}

export const api = {
  health: () => request<{ status: string }>("/healthz"),
  authSession: () => request<{ authenticated: boolean; user: AuthUser | null }>("/api/auth/session"),
  login: (username: string, password: string) =>
    request<{ user: AuthUser; expiresAt: number }>("/api/auth/login", {
      method: "POST", body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ status: string }>("/api/auth/logout", { method: "POST" }),
  listInstances: async () => (await request<{ instances: AgentInstance[] }>("/api/instances")).instances,
  createInstance: (id?: string) =>
    request<AgentInstance>("/api/instances", {
      method: "POST",
      body: JSON.stringify(id ? { id } : {}),
    }),
  startInstance: (id: string) => request<AgentInstance>(`/api/instances/${id}/start`, { method: "POST" }),
  stopInstance: (id: string) => request<AgentInstance>(`/api/instances/${id}/stop`, { method: "POST" }),
  destroyInstance: (id: string) => request<{ id: string; status: string }>(`/api/instances/${id}`, { method: "DELETE" }),
  updateAgentConfig: (
    id: string,
    config: { agentName: string; systemPrompt: string; toolMode: "read_only" | "read_write" },
  ) => request<AgentInstance>(`/api/instances/${id}/config`, {
    method: "POST", body: JSON.stringify(config),
  }),
  createTask: (instanceId: string, prompt: string) =>
    request<AgentTask>(`/api/instances/${instanceId}/tasks`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  getTask: (instanceId: string, taskId: string) =>
    request<AgentTask>(`/api/instances/${instanceId}/tasks/${taskId}`),
  streamTask: (instanceId: string, taskId: string) =>
    new EventSource(`/api/instances/${instanceId}/tasks/${taskId}/stream`),
  listConversations: async (instanceId: string) =>
    (
      await request<{ conversations: AgentConversation[] }>(
        `/api/instances/${instanceId}/conversations`,
      )
    ).conversations,
  createConversation: (instanceId: string) =>
    request<AgentConversation>(`/api/instances/${instanceId}/conversations`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  listMessages: async (instanceId: string, conversationId: string) =>
    (
      await request<{ messages: AgentMessage[] }>(
        `/api/instances/${instanceId}/conversations/${conversationId}/messages`,
      )
    ).messages,
  createConversationMessage: (instanceId: string, conversationId: string, content: string) =>
    request<AgentTask>(
      `/api/instances/${instanceId}/conversations/${conversationId}/messages`,
      {
        method: "POST",
        body: JSON.stringify({ content }),
      },
    ),
  listWorkspace: (instanceId: string, path = "") =>
    request<WorkspaceListing>(`/api/instances/${instanceId}/workspace/list`, {
      method: "POST", body: JSON.stringify({ path }),
    }),
  readWorkspaceFile: (instanceId: string, path: string) =>
    request<WorkspaceFile>(`/api/instances/${instanceId}/workspace/read`, {
      method: "POST", body: JSON.stringify({ path }),
    }),
  writeWorkspaceFile: (instanceId: string, path: string, contentBase64: string) =>
    request<{ path: string; size: number }>(`/api/instances/${instanceId}/workspace/write`, {
      method: "POST", body: JSON.stringify({ path, contentBase64 }),
    }),
  createWorkspaceDirectory: (instanceId: string, path: string) =>
    request<{ path: string }>(`/api/instances/${instanceId}/workspace/mkdir`, {
      method: "POST", body: JSON.stringify({ path }),
    }),
  deleteWorkspaceEntry: (instanceId: string, path: string) =>
    request<{ path: string }>(`/api/instances/${instanceId}/workspace/delete`, {
      method: "POST", body: JSON.stringify({ path }),
    }),
};
