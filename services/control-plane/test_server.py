#!/usr/bin/env python3
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
    def __init__(self) -> None:
        self.instances: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def start(self, instance_id: str) -> dict:
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
        status = self.instances.get(instance_id)
        return None if status is None else dict(status)

    def heartbeat(self, instance_id: str) -> dict:
        self.calls.append(("heartbeat", instance_id))
        self.instances[instance_id]["idleSeconds"] = 0
        return dict(self.instances[instance_id])

    def stop(self, instance_id: str) -> dict:
        self.calls.append(("stop", instance_id))
        self.instances[instance_id].update(status="stopped", processAlive=False)
        return dict(self.instances[instance_id])

    def destroy(self, instance_id: str) -> None:
        self.calls.append(("destroy", instance_id))
        self.instances.pop(instance_id, None)


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = Path(self.temporary.name) / "control-plane.db"
        self.store = SERVER.InstanceStore(database)
        self.runtime = FakeRuntime()
        self.control = SERVER.ControlPlane(self.store, self.runtime)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_full_lifecycle(self) -> None:
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
        self.control.create({"id": "valid-id"})
        with self.assertRaises(SERVER.ApiError) as duplicate:
            self.control.create({"id": "valid-id"})
        self.assertEqual(duplicate.exception.status, 409)

        for invalid in ("UPPER", "bad_id", "-leading", "a" * 64):
            with self.assertRaises(SERVER.ApiError):
                self.control.create({"id": invalid})

    def test_generated_id_and_listing(self) -> None:
        generated = self.control.create({})
        self.assertRegex(generated["id"], r"^agent-[0-9a-f]{12}$")
        self.assertEqual(len(self.control.list()), 1)

    def test_dead_runtime_is_reconciled(self) -> None:
        self.control.create({"id": "agent-dead"})
        self.control.start("agent-dead")
        self.runtime.instances["agent-dead"].update(status="stopped", processAlive=False)
        reconciled = self.control.get("agent-dead")
        self.assertEqual(reconciled["status"], "stopped")

    def test_invalid_transition_is_rejected(self) -> None:
        self.control.create({"id": "agent-created"})
        with self.assertRaises(SERVER.ApiError) as stop_error:
            self.control.stop("agent-created")
        self.assertEqual(stop_error.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
