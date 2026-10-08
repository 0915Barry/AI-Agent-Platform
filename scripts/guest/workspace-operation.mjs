#!/usr/bin/env node
/**
 * 在 microVM 内以 pi 用户执行单次工作区文件操作。
 *
 * 宿主只负责排队，真正的读写发生在 /workspace。所有路径都必须是相对路径；
 * 已存在路径中的符号链接会被拒绝，防止借助软链接越过持久化数据盘边界。
 */

import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const MAX_FILE_BYTES = 5 * 1024 * 1024;
const MAX_LIST_ENTRIES = 500;

function validateRelativePath(value, allowRoot = false) {
  if (typeof value !== "string") throw new Error("workspace path must be a string");
  if (value.startsWith("/")) throw new Error("workspace path must be relative");
  const clean = value.replace(/^\/+|\/+$/g, "");
  if (!clean) {
    if (allowRoot) return "";
    throw new Error("workspace path must not be empty");
  }
  if (clean.includes("\\") || clean.includes("\0")) throw new Error("invalid workspace path");
  const parts = clean.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    throw new Error("workspace path contains an invalid segment");
  }
  return parts.join("/");
}

function targetFor(root, relative) {
  const target = path.resolve(root, relative);
  if (target !== root && !target.startsWith(`${root}${path.sep}`)) {
    throw new Error("workspace path escapes the workspace");
  }
  return target;
}

async function syncDirectory(directory) {
  // Firecracker stop 会直接结束 VMM，不等同于 guest 正常关机。目录 fsync 确保
  // 新建/删除的目录项在回报成功前已经进入持久化数据盘。
  const handle = await fs.open(directory, "r");
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

async function ensureSafeSegments(root, relative, createDirectories = false) {
  let current = root;
  const parts = relative ? relative.split("/") : [];
  for (const part of parts) {
    const parent = current;
    current = path.join(current, part);
    try {
      const stats = await fs.lstat(current);
      if (stats.isSymbolicLink()) throw new Error("symbolic links are not allowed");
      if (current !== targetFor(root, relative) && !stats.isDirectory()) {
        throw new Error("parent path is not a directory");
      }
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      if (!createDirectories) throw error;
      await fs.mkdir(current, { mode: 0o755 });
      await syncDirectory(parent);
    }
  }
  return current;
}

export async function executeWorkspaceOperation(operation, rootOverride) {
  const root = path.resolve(rootOverride || process.env.WORKSPACE_ROOT || "/workspace");
  const action = operation?.action;
  const relative = validateRelativePath(operation?.path ?? "", action === "list");
  const target = targetFor(root, relative);

  if (action === "list") {
    await ensureSafeSegments(root, relative);
    const stats = await fs.lstat(target);
    if (!stats.isDirectory()) throw new Error("workspace path is not a directory");
    const names = (await fs.readdir(target)).sort((a, b) => a.localeCompare(b, "zh-CN"));
    if (names.length > MAX_LIST_ENTRIES) throw new Error("directory contains more than 500 entries");
    const entries = [];
    for (const name of names) {
      const childRelative = relative ? `${relative}/${name}` : name;
      const child = await fs.lstat(targetFor(root, childRelative));
      entries.push({
        name,
        path: childRelative,
        type: child.isDirectory() ? "directory" : child.isFile() ? "file" : "unsupported",
        size: child.isFile() ? child.size : 0,
        modifiedAt: Math.floor(child.mtimeMs / 1000),
      });
    }
    return { path: relative, entries };
  }

  if (action === "read") {
    await ensureSafeSegments(root, relative);
    const stats = await fs.lstat(target);
    if (!stats.isFile()) throw new Error("workspace path is not a regular file");
    if (stats.size > MAX_FILE_BYTES) throw new Error("workspace file exceeds 5 MiB");
    const content = await fs.readFile(target);
    return { path: relative, name: path.basename(relative), size: content.length, contentBase64: content.toString("base64") };
  }

  if (action === "write") {
    const encoded = operation?.contentBase64;
    if (typeof encoded !== "string") throw new Error("contentBase64 must be a string");
    const content = Buffer.from(encoded, "base64");
    if (content.length > MAX_FILE_BYTES) throw new Error("workspace file exceeds 5 MiB");
    const parent = path.posix.dirname(relative);
    if (parent !== ".") await ensureSafeSegments(root, parent, true);
    try {
      const existing = await fs.lstat(target);
      if (existing.isSymbolicLink() || !existing.isFile()) throw new Error("target is not a regular file");
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    const handle = await fs.open(target, "w", 0o644);
    try {
      await handle.writeFile(content);
      await handle.sync();
    } finally {
      await handle.close();
    }
    await syncDirectory(path.dirname(target));
    return { path: relative, size: content.length };
  }

  if (action === "mkdir") {
    await ensureSafeSegments(root, relative, true);
    return { path: relative };
  }

  if (action === "delete") {
    await ensureSafeSegments(root, relative);
    const stats = await fs.lstat(target);
    if (stats.isFile()) await fs.unlink(target);
    else if (stats.isDirectory()) await fs.rmdir(target); // 仅允许删除空目录。
    else throw new Error("unsupported workspace entry type");
    await syncDirectory(path.dirname(target));
    return { path: relative };
  }

  throw new Error("unsupported workspace action");
}

async function main() {
  const inputPath = process.argv[2];
  if (!inputPath) throw new Error("operation JSON path is required");
  const operation = JSON.parse(await fs.readFile(inputPath, "utf8"));
  try {
    const result = await executeWorkspaceOperation(operation);
    process.stdout.write(JSON.stringify({ status: "completed", result }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ status: "failed", error: String(error.message || error).slice(0, 4000) }));
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stdout.write(JSON.stringify({ status: "failed", error: String(error.message || error).slice(0, 4000) }));
  });
}
