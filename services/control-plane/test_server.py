#!/usr/bin/env python3
"""M10-M13 控制面、任务队列与多轮会话的纯本地单元测试。

测试使用 :class:`FakeRuntime` 替代真正的 Firecracker 运行时，因此不要求
``/dev/kvm``、root 权限或 Linux 虚拟机。这里主要验证控制面的业务规则：

* 实例从创建、启动、心跳、停止到销毁的完整状态流转；
* 重复 ID、非法 ID 和非法状态转换会被拒绝；
* 数据库状态会根据运行时进程的实际状态进行校正。

真正启动 microVM 的端到端验证由 ``./run.sh control-plane-smoke-test`` 完成。
"""

import importlib.util
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().with_name("server.py")
SPEC = importlib.util.spec_from_file_location("agent_control_plane_server", SERVER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load control-plane server")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class FakeRuntime:
    """只在内存中模拟实例状态的运行时，隔离控制面与 Firecracker 细节。"""

    def __init__(self) -> None:
        # instances 保存运行时视角的当前状态；calls 便于后续断言调用顺序。
        self.instances: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def start(self, instance_id: str) -> dict:
        """模拟启动实例并返回与真实 Runtime 相同形状的状态数据。"""

        self.calls.append(("start", instance_id))
        status = {
            "id": instance_id,
            "status": "running",
            "processAlive": True,
            "idleSeconds": 0,
        }
        self.instances[instance_id] = status
        return status

    def status(self, instance_id: str) -> dict | None:
        """返回状态副本，避免测试代码意外修改模拟运行时的内部数据。"""

        status = self.instances.get(instance_id)
        return None if status is None else dict(status)

    def heartbeat(self, instance_id: str) -> dict:
        """模拟一次活动心跳，将空闲时间归零。"""

        self.calls.append(("heartbeat", instance_id))
        self.instances[instance_id]["idleSeconds"] = 0
        return dict(self.instances[instance_id])

    def stop(self, instance_id: str) -> dict:
        """模拟停止计算资源，但保留实例记录。"""

        self.calls.append(("stop", instance_id))
        self.instances[instance_id].update(status="stopped", processAlive=False)
        return dict(self.instances[instance_id])

    def destroy(self, instance_id: str) -> None:
        """模拟彻底销毁实例及其运行时状态。"""

        self.calls.append(("destroy", instance_id))
        self.instances.pop(instance_id, None)


class ControlPlaneTests(unittest.TestCase):
    """验证 ControlPlane 的状态机、输入校验和持久化协调逻辑。"""

    def setUp(self) -> None:
        """为每个用例创建独立 SQLite 数据库，避免测试之间相互污染。"""

        self.temporary = tempfile.TemporaryDirectory()
        database = Path(self.temporary.name) / "control-plane.db"
        task_database = Path(self.temporary.name) / "tasks.db"
        self.store = SERVER.InstanceStore(database)
        self.tasks = SERVER.TaskStore(task_database)
        self.runtime = FakeRuntime()
        self.control = SERVER.ControlPlane(self.store, self.runtime, self.tasks)

    def tearDown(self) -> None:
        """清理本用例的临时数据库目录。"""

        self.temporary.cleanup()

    def test_full_lifecycle(self) -> None:
        """完整生命周期应能按 created -> running -> stopped -> destroyed 流转。"""

        created = self.control.create({"id": "agent-test"})
        self.assertEqual(created["status"], "created")

        running = self.control.start("agent-test")
        self.assertEqual(running["status"], "running")
        self.assertTrue(running["runtime"]["processAlive"])

        heartbeat = self.control.heartbeat("agent-test")
        self.assertEqual(heartbeat["runtime"]["idleSeconds"], 0)

        stopped = self.control.stop("agent-test")
        self.assertEqual(stopped["status"], "stopped")

        destroyed = self.control.destroy("agent-test")
        self.assertEqual(destroyed, {"id": "agent-test", "status": "destroyed"})
        with self.assertRaises(SERVER.ApiError) as missing:
            self.control.get("agent-test")
        self.assertEqual(missing.exception.status, 404)

    def test_duplicate_and_invalid_ids_are_rejected(self) -> None:
        """ID 必须符合安全字符规则且在数据库中唯一。"""

        self.control.create({"id": "valid-id"})
        with self.assertRaises(SERVER.ApiError) as duplicate:
            self.control.create({"id": "valid-id"})
        self.assertEqual(duplicate.exception.status, 409)

        for invalid in ("UPPER", "bad_id", "-leading", "a" * 64):
            with self.assertRaises(SERVER.ApiError):
                self.control.create({"id": invalid})

    def test_generated_id_and_listing(self) -> None:
        """未提供 ID 时应自动生成可用 ID，并能通过列表接口查询。"""

        generated = self.control.create({})
        self.assertRegex(generated["id"], r"^agent-[0-9a-f]{12}$")
        self.assertEqual(len(self.control.list()), 1)

    def test_dead_runtime_is_reconciled(self) -> None:
        """运行时进程已退出时，控制面不能继续把实例报告为 running。"""

        self.control.create({"id": "agent-dead"})
        self.control.start("agent-dead")
        self.runtime.instances["agent-dead"].update(status="stopped", processAlive=False)
        reconciled = self.control.get("agent-dead")
        self.assertEqual(reconciled["status"], "stopped")

    def test_invalid_transition_is_rejected(self) -> None:
        """尚未启动的 created 实例不允许执行 stop。"""

        self.control.create({"id": "agent-created"})
        with self.assertRaises(SERVER.ApiError) as stop_error:
            self.control.stop("agent-created")
        self.assertEqual(stop_error.exception.status, 409)

    def test_task_lifecycle_and_events(self) -> None:
        """运行实例可以创建任务，guest 领取后能回传最终结果和事件。"""

        self.control.create({"id": "agent-task"})
        self.control.start("agent-task")
        queued = self.control.create_task("agent-task", {"prompt": "read the marker"})
        self.assertEqual(queued["status"], "queued")

        claimed = self.tasks.claim_next("agent-task")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["status"], "running")
        self.tasks.append_event(
            "agent-task",
            claimed["id"],
            "completed",
            {"output": "M11_OK"},
        )

        completed = self.control.get_task("agent-task", claimed["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["output"], "M11_OK")
        event_types = [event["type"] for event in self.control.task_events("agent-task", claimed["id"])]
        self.assertEqual(event_types, ["queued", "started", "completed"])

    def test_sse_replays_incremental_events_and_closes_at_terminal_state(self) -> None:
        """SSE 应按序重放增量事件，并在 completed 后结束 HTTP 响应。"""

        self.control.create({"id": "agent-stream"})
        self.control.start("agent-stream")
        queued = self.control.create_task("agent-stream", {"prompt": "stream this"})
        claimed = self.tasks.claim_next("agent-stream")
        assert claimed is not None
        self.tasks.append_event(
            "agent-stream", claimed["id"], "progress", {"stage": "model_started"}
        )
        self.tasks.append_event("agent-stream", claimed["id"], "text", {"delta": "hello"})
        self.tasks.append_event(
            "agent-stream", claimed["id"], "completed", {"output": "hello"}
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), SERVER.ControlPlaneHandler)
        server.control = self.control  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}/api/instances/agent-stream/"
                f"tasks/{queued['id']}/stream"
            )
            with urllib.request.urlopen(url, timeout=3) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            self.assertIn("event: task-event", body)
            self.assertIn('"type":"text"', body)
            self.assertIn('"type":"completed"', body)
            event_ids = [
                int(line.removeprefix("id: "))
                for line in body.splitlines()
                if line.startswith("id: ")
            ]
            self.assertEqual(event_ids, sorted(event_ids))
            self.assertEqual(len(event_ids), len(set(event_ids)))

            resume_request = urllib.request.Request(
                url, headers={"Last-Event-ID": str(event_ids[1])}
            )
            with urllib.request.urlopen(resume_request, timeout=3) as response:
                resumed = response.read().decode("utf-8")
            self.assertNotIn('"type":"queued"', resumed)
            self.assertNotIn('"type":"started"', resumed)
            self.assertIn('"type":"progress"', resumed)
            self.assertIn('"type":"completed"', resumed)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_task_requires_running_instance_and_valid_prompt(self) -> None:
        """任务只能发送给 running 实例，且提示词不能为空。"""

        self.control.create({"id": "agent-idle"})
        with self.assertRaises(SERVER.ApiError) as not_running:
            self.control.create_task("agent-idle", {"prompt": "hello"})
        self.assertEqual(not_running.exception.status, 409)

        self.control.start("agent-idle")
        with self.assertRaises(SERVER.ApiError) as empty_prompt:
            self.control.create_task("agent-idle", {"prompt": "   "})
        self.assertEqual(empty_prompt.exception.status, 400)

    def test_conversation_persists_messages_and_builds_context(self) -> None:
        """第二轮任务应包含首轮问答，完成结果应写回消息历史。"""

        self.control.create({"id": "agent-chat"})
        self.control.start("agent-chat")
        conversation = self.control.create_conversation("agent-chat", {})

        first = self.control.create_conversation_message(
            "agent-chat", conversation["id"], {"content": "请记住我的代号是 Alpha"}
        )
        claimed_first = self.tasks.claim_next("agent-chat")
        assert claimed_first is not None
        self.tasks.append_event(
            "agent-chat", claimed_first["id"], "completed", {"output": "好的，我会记住。"}
        )

        second = self.control.create_conversation_message(
            "agent-chat", conversation["id"], {"content": "我的代号是什么？"}
        )
        self.assertEqual(second["conversationId"], conversation["id"])
        self.assertIn("Alpha", second["prompt"])
        self.assertIn("我的代号是什么", second["prompt"])
        self.assertNotEqual(first["id"], second["id"])

        messages = self.control.conversation_messages("agent-chat", conversation["id"])
        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[1]["content"], "好的，我会记住。")
        listed = self.control.list_conversations("agent-chat")
        self.assertEqual(listed[0]["title"], "请记住我的代号是 Alpha")

    def test_conversation_is_isolated_and_deleted_with_instance(self) -> None:
        """会话不能跨实例读取，显式销毁实例必须清除消息。"""

        self.control.create({"id": "agent-one"})
        self.control.create({"id": "agent-two"})
        conversation = self.control.create_conversation("agent-one", {})
        with self.assertRaises(SERVER.ApiError) as cross_instance:
            self.control.conversation_messages("agent-two", conversation["id"])
        self.assertEqual(cross_instance.exception.status, 404)

        self.control.destroy("agent-one")
        self.assertEqual(self.tasks.list_conversations("agent-one"), [])

    def test_existing_m11_task_database_is_migrated(self) -> None:
        """旧任务表升级 M13 时应保留数据并增加 conversation_id。"""

        legacy_path = Path(self.temporary.name) / "legacy-tasks.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, prompt TEXT NOT NULL,
                    status TEXT NOT NULL, output TEXT, error TEXT,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                ("task-0000000000000000", "agent-old", "hello", "completed", "ok", None, 1, 2),
            )
            connection.commit()
        migrated = SERVER.TaskStore(legacy_path)
        task = migrated.get("agent-old", "task-0000000000000000")
        self.assertEqual(task["output"], "ok")
        self.assertIsNone(task["conversationId"])

    def test_workspace_operation_is_scoped_and_returns_guest_result(self) -> None:
        """文件请求只能被目标实例领取，控制面应等待并返回 guest 结果。"""

        self.control.create({"id": "agent-files"})
        self.control.create({"id": "agent-other"})
        self.control.start("agent-files")

        def guest() -> None:
            operation = None
            for _ in range(50):
                operation = self.tasks.claim_next_workspace_operation("agent-files")
                if operation:
                    break
                time.sleep(0.01)
            assert operation is not None
            self.assertEqual(operation["path"], "docs/readme.txt")
            self.tasks.finish_workspace_operation(
                "agent-files", operation["id"], "completed", {"path": operation["path"], "size": 5}
            )

        worker = threading.Thread(target=guest)
        worker.start()
        result = self.control.workspace_operation(
            "agent-files", "write", {"path": "docs/readme.txt", "contentBase64": "aGVsbG8="}
        )
        worker.join()
        self.assertEqual(result, {"path": "docs/readme.txt", "size": 5})
        self.assertIsNone(self.tasks.claim_next_workspace_operation("agent-other"))

    def test_workspace_rejects_traversal_and_large_files(self) -> None:
        """控制面在请求进入 guest 前拒绝目录穿越与超过 5 MiB 的内容。"""

        self.control.create({"id": "agent-files"})
        self.control.start("agent-files")
        for unsafe in ("../secret", "folder/../secret", "/etc/shadow", "folder\\secret"):
            with self.assertRaises(SERVER.ApiError) as rejected:
                self.control.workspace_operation("agent-files", "read", {"path": unsafe})
            self.assertEqual(rejected.exception.status, 400)
        oversized = "A" * (((5 * 1024 * 1024 + 1) * 4 + 2) // 3)
        with self.assertRaises(SERVER.ApiError) as too_large:
            self.control.workspace_operation(
                "agent-files", "write", {"path": "large.bin", "contentBase64": oversized}
            )
        self.assertIn(too_large.exception.status, {400, 413})


if __name__ == "__main__":
    unittest.main()
