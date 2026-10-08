import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { executeWorkspaceOperation } from "./workspace-operation.mjs";

test("工作区支持目录、写入、列表、读取和删除", async (context) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "aap-workspace-"));
  context.after(() => fs.rm(root, { recursive: true, force: true }));
  await executeWorkspaceOperation({ action: "mkdir", path: "docs" }, root);
  await executeWorkspaceOperation(
    { action: "write", path: "docs/hello.txt", contentBase64: Buffer.from("你好 M14").toString("base64") },
    root,
  );
  const listing = await executeWorkspaceOperation({ action: "list", path: "docs" }, root);
  assert.equal(listing.entries[0].name, "hello.txt");
  const read = await executeWorkspaceOperation({ action: "read", path: "docs/hello.txt" }, root);
  assert.equal(Buffer.from(read.contentBase64, "base64").toString(), "你好 M14");
  await executeWorkspaceOperation({ action: "delete", path: "docs/hello.txt" }, root);
  await executeWorkspaceOperation({ action: "delete", path: "docs" }, root);
});

test("工作区拒绝路径穿越、符号链接和非空目录删除", async (context) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "aap-workspace-"));
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "aap-outside-"));
  context.after(async () => {
    await fs.rm(root, { recursive: true, force: true });
    await fs.rm(outside, { recursive: true, force: true });
  });
  await assert.rejects(() => executeWorkspaceOperation({ action: "read", path: "../secret" }, root));
  await assert.rejects(() => executeWorkspaceOperation({ action: "read", path: "/etc/shadow" }, root));
  await fs.symlink(outside, path.join(root, "escape"));
  await assert.rejects(() => executeWorkspaceOperation({ action: "list", path: "escape" }, root));
  await fs.mkdir(path.join(root, "not-empty"));
  await fs.writeFile(path.join(root, "not-empty", "file"), "data");
  await assert.rejects(() => executeWorkspaceOperation({ action: "delete", path: "not-empty" }, root));
});
