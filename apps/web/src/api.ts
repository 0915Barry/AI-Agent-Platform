export type InstanceStatus = "created" | "starting" | "running" | "stopping" | "stopped" | "failed";
export type TaskStatus = "queued" | "running" | "completed" | "failed";

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
}

interface ApiErrorPayload {
  error?: { code?: string; message?: string };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const payload = (await response.json().catch(() => ({}))) as T & ApiErrorPayload;
  if (!response.ok) {
    throw new Error(payload.error?.message ?? `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

export const api = {
  health: () => request<{ status: string }>("/healthz"),
  listInstances: async () => (await request<{ instances: AgentInstance[] }>("/api/instances")).instances,
  createInstance: (id?: string) =>
    request<AgentInstance>("/api/instances", {
      method: "POST",
      body: JSON.stringify(id ? { id } : {}),
    }),
  startInstance: (id: string) => request<AgentInstance>(`/api/instances/${id}/start`, { method: "POST" }),
  stopInstance: (id: string) => request<AgentInstance>(`/api/instances/${id}/stop`, { method: "POST" }),
  destroyInstance: (id: string) => request<{ id: string; status: string }>(`/api/instances/${id}`, { method: "DELETE" }),
  createTask: (instanceId: string, prompt: string) =>
    request<AgentTask>(`/api/instances/${instanceId}/tasks`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  getTask: (instanceId: string, taskId: string) =>
    request<AgentTask>(`/api/instances/${instanceId}/tasks/${taskId}`),
};
