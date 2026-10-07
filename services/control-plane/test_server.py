#!/usr/bin/env python3
"""M10 控制面的纯本地单元测试。

测试使用 :class:`FakeRuntime` 替代真正的 Firecracker 运行时，因此不要求
``/dev/kvm``、root 权限或 Linux 虚拟机。这里主要验证控制面的业务规则：

* 实例从创建、启动、心跳、停止到销毁的完整状态流转；
* 重复 ID、非法 ID 和非法状态转换会被拒绝；
* 数据库状态会根据运行时进程的实际状态进行校正。

真正启动 microVM 的端到端验证由 ``./run.sh control-plane-smoke-test`` 完成。
"""

import importlib.util
import tempfile
import unittest
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
        self.store = SERVER.InstanceStore(database)
        self.runtime = FakeRuntime()
        self.control = SERVER.ControlPlane(self.store, self.runtime)

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


if __name__ == "__main__":
    unittest.main()
