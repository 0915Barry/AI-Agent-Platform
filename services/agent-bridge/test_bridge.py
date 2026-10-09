#!/usr/bin/env python3
"""Agent Bridge 任务快照边界测试。"""

import importlib.util
import unittest
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().with_name("bridge.py")
SPEC = importlib.util.spec_from_file_location("agent_bridge", BRIDGE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load Agent Bridge")
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class BridgePayloadTests(unittest.TestCase):
    def test_guest_payload_includes_reviewed_dynamic_capability_snapshots(self) -> None:
        task = {
            "id": "task-0123456789abcdef",
            "prompt": "test",
            "systemPrompt": "system",
            "toolMode": "read_only",
            "skills": [{"id": "document-summary", "content": "reviewed"}],
            "mcps": [{
                "id": "platform-records",
                "serverName": "platform_records",
                "description": "reviewed MCP",
            }],
            "status": "running",
            "ownerId": "must-not-leak",
        }

        payload = BRIDGE.guest_task_payload(task)

        self.assertEqual(payload["mcps"], task["mcps"])
        self.assertEqual(payload["skills"], task["skills"])
        self.assertNotIn("ownerId", payload)
        self.assertNotIn("status", payload)


if __name__ == "__main__":
    unittest.main()
