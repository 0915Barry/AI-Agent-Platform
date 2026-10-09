#!/usr/bin/env node
/**
 * 在 microVM 内运行一次 Pi JSON 模式，并把结构化增量事件回传给宿主 bridge。
 *
 * 这里刻意不转发 thinking_delta：模型内部思考不属于产品输出。文本增量会按短时间窗
 * 合并，既让页面尽快看到首字，又避免每个 token 都产生一次 HTTP/SQLite 写入。
 */

import fs from "node:fs/promises";
import { spawn } from "node:child_process";

const taskPath = process.argv[2];
const bridgeUrl = process.env.AGENT_BRIDGE_URL;
const bridgeToken = process.env.AGENT_BRIDGE_TOKEN;
const mcpUrl = process.env.AGENT_MCP_URL;
const mcpToken = process.env.AGENT_MCP_TOKEN;

if (!taskPath || !bridgeUrl || !bridgeToken) {
  throw new Error("task path and bridge environment are required");
}

const task = JSON.parse(await fs.readFile(taskPath, "utf8"));
if (!/^task-[0-9a-f]{16}$/.test(task.id) || typeof task.prompt !== "string" || !task.prompt) {
  throw new Error("invalid task payload");
}
if (typeof task.systemPrompt !== "string" || !task.systemPrompt ||
    !["read_only", "read_write"].includes(task.toolMode)) {
  throw new Error("invalid Agent configuration");
}
if (!Array.isArray(task.skills) || task.skills.length > 5 || task.skills.some((skill) =>
  !skill || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(skill.id) ||
  typeof skill.content !== "string" || Buffer.byteLength(skill.content, "utf8") > 32768)) {
  throw new Error("invalid Skill snapshot");
}
if (!Array.isArray(task.mcps) || task.mcps.length > 3 || task.mcps.some((mcp) =>
  !mcp || mcp.id !== "platform-records" || mcp.serverName !== "platform_records" ||
  typeof mcp.description !== "string" || Buffer.byteLength(mcp.description, "utf8") > 1024)) {
  throw new Error("invalid MCP snapshot");
}
if (task.mcps.length > 0 && (!mcpUrl || !mcpToken)) {
  throw new Error("MCP environment is required by this task");
}

const fileTools = task.toolMode === "read_only" ? ["read"] : ["read", "write"];
const mcpTools = task.mcps.map((mcp) => `mcp__${mcp.serverName}__get_verification_record`);
const enabledTools = [...fileTools, ...mcpTools].join(",");
// Pi 的 --system-prompt 同时接受文本或路径。写入一个固定权限的临时文件可以保证
// 用户输入即使恰好像现有路径，也始终按提示词正文处理，而不会被解释成任意文件。
const systemPromptPath = `/tmp/pi-system-prompt-${process.pid}.txt`;
await fs.writeFile(systemPromptPath, task.systemPrompt, { encoding: "utf8", mode: 0o600 });
const skillsRoot = `/tmp/pi-skills-${process.pid}`;
const skillArguments = [];
for (const skill of task.skills) {
  const skillDirectory = `${skillsRoot}/${skill.id}`;
  await fs.mkdir(skillDirectory, { recursive: true, mode: 0o700 });
  await fs.writeFile(`${skillDirectory}/SKILL.md`, skill.content, { encoding: "utf8", mode: 0o600 });
  skillArguments.push("--skill", skillDirectory);
}

// 使用临时 HOME 注入任务专属的审核 MCP 配置。持久数据盘中的 models.json 被复制
// 进来，但 mcp.json 和一次性访问令牌不会写回 /workspace 或跨任务残留。
const piHome = `/tmp/pi-home-${process.pid}`;
await fs.mkdir(`${piHome}/.pi/agent`, { recursive: true, mode: 0o700 });
await fs.copyFile("/home/pi/.pi/agent/models.json", `${piHome}/.pi/agent/models.json`);
if (task.mcps.length > 0) {
  const mcpServers = Object.fromEntries(task.mcps.map((mcp) => [mcp.serverName, {
    type: "streamable-http",
    url: mcpUrl,
    headers: { Authorization: "Bearer ${AGENT_MCP_TOKEN}" },
    description: mcp.description,
    exposure: "direct",
    timeout: 15,
  }]));
  await fs.writeFile(
    `${piHome}/.pi/agent/mcp.json`,
    `${JSON.stringify({ mcpServers })}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
}
const extensionArguments = task.mcps.length > 0 ? ["--extension", "builtin:mcp"] : [];

async function postEvent(type, data = {}) {
  const response = await fetch(`${bridgeUrl}/guest/tasks/${task.id}/events`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${bridgeToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ type, ...data }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 500);
    throw new Error(`bridge rejected ${type}: HTTP ${response.status} ${detail}`);
  }
}

function messageText(message) {
  if (!message || message.role !== "assistant" || !Array.isArray(message.content)) return "";
  return message.content
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
}

function compactToolResult(result) {
  const text = Array.isArray(result?.content)
    ? result.content
        .filter((part) => part?.type === "text" && typeof part.text === "string")
        .map((part) => part.text)
        .join("\n")
    : "";
  return text.slice(0, 2000);
}

const pi = spawn(
  "/usr/local/bin/pi",
  [
    "--offline",
    "--no-session",
    "--no-approve",
    "--no-extensions",
    ...extensionArguments,
    "--no-skills",
    ...skillArguments,
    "--no-prompt-templates",
    "--no-context-files",
    "--tools",
    enabledTools,
    "--system-prompt",
    systemPromptPath,
    "--provider",
    "deepseek-gateway",
    "--model",
    "deepseek-flash",
    "--mode",
    "json",
    task.prompt,
  ],
  {
    cwd: "/workspace",
    env: { ...process.env, HOME: piHome, PI_SKIP_VERSION_CHECK: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  },
);

let stdoutBuffer = Buffer.alloc(0);
let stderr = "";
let finalOutput = "";
let terminalError = "";
let settled = false;
let pendingText = "";
let lastTextFlush = 0;

// stderr 必须和 stdout 并行排空，否则大量诊断信息可能填满管道并阻塞 Pi。
const stderrDone = (async () => {
  for await (const chunk of pi.stderr) {
    stderr = (stderr + chunk.toString("utf8")).slice(-4000);
  }
})();

async function flushText(force = false) {
  if (!pendingText) return;
  const now = Date.now();
  if (!force && pendingText.length < 512 && now - lastTextFlush < 100) return;
  const delta = pendingText;
  pendingText = "";
  lastTextFlush = now;
  await postEvent("text", { delta });
}

async function consumeEvent(event) {
  if (!event || typeof event.type !== "string") return;
  if (event.type === "agent_start") {
    await postEvent("progress", { stage: "model_started" });
    return;
  }
  if (event.type === "message_update") {
    const update = event.assistantMessageEvent;
    if (update?.type === "text_delta" && typeof update.delta === "string") {
      pendingText += update.delta;
      await flushText(false);
    }
    return;
  }
  await flushText(true);
  if (event.type === "tool_execution_start") {
    await postEvent("tool_call", {
      toolCallId: event.toolCallId,
      toolName: event.toolName,
      args: event.args,
    });
  } else if (event.type === "tool_execution_end") {
    await postEvent("tool_result", {
      toolCallId: event.toolCallId,
      toolName: event.toolName,
      isError: Boolean(event.isError),
      preview: compactToolResult(event.result),
    });
  } else if (event.type === "auto_retry_start") {
    await postEvent("progress", {
      stage: "retrying",
      attempt: event.attempt,
      delayMs: event.delayMs,
    });
  } else if (event.type === "message_end") {
    const output = messageText(event.message);
    if (output) finalOutput = output;
    if (event.message?.stopReason === "error" || event.message?.stopReason === "aborted") {
      terminalError = String(event.message.errorMessage || `Pi ${event.message.stopReason}`);
    }
  } else if (event.type === "agent_settled") {
    settled = true;
  }
}

// 严格按 LF 拆分 JSONL；不能用 readline，因为 Unicode 行分隔符在 JSON 字符串中合法。
for await (const chunk of pi.stdout) {
  stdoutBuffer = Buffer.concat([stdoutBuffer, chunk]);
  while (true) {
    const newline = stdoutBuffer.indexOf(0x0a);
    if (newline < 0) break;
    let line = stdoutBuffer.subarray(0, newline);
    stdoutBuffer = stdoutBuffer.subarray(newline + 1);
    if (line.at(-1) === 0x0d) line = line.subarray(0, -1);
    if (!line.length) continue;
    await consumeEvent(JSON.parse(line.toString("utf8")));
  }
}

const exitCode = await new Promise((resolve) => {
  if (pi.exitCode !== null) resolve(pi.exitCode);
  else pi.once("close", resolve);
});
await stderrDone;
await flushText(true);
await fs.unlink(systemPromptPath).catch(() => undefined);
await fs.rm(skillsRoot, { recursive: true, force: true }).catch(() => undefined);
await fs.rm(piHome, { recursive: true, force: true }).catch(() => undefined);

if (exitCode !== 0 || terminalError || !settled) {
  await postEvent("failed", {
    error: (terminalError || stderr || `Pi exited with status ${exitCode}; settled=${settled}`).slice(0, 4000),
  });
  process.exitCode = 1;
} else {
  await postEvent("completed", { output: finalOutput });
}
