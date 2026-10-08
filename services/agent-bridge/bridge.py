#!/usr/bin/env python3
"""仅绑定单个 TAP 地址的 guest 任务桥。

microVM 通过短轮询领取任务并回传事件。桥接服务不持有模型供应商密钥，也不
执行用户命令；它只访问宿主 SQLite 队列。随机实例令牌用于防止同一宿主上的
其他 guest 冒充当前实例，nftables 继续负责网络层隔离。
"""

import argparse
import hmac
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "control-plane"))

from task_store import TaskStore, TaskStoreError  # noqa: E402


TASK_EVENT_ROUTE = re.compile(r"^/guest/tasks/(task-[0-9a-f]{16})/events$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", type=int, default=18083)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--activity-file", required=True, type=Path)
    parser.add_argument("--audit-file", required=True, type=Path)
    args = parser.parse_args()

    expected_token = args.token_file.read_text(encoding="utf-8").strip()
    if not expected_token:
        raise SystemExit("bridge token is empty")
    store = TaskStore(args.database)

    def audit(event: dict) -> None:
        with args.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    class BridgeHandler(BaseHTTPRequestHandler):
        """认证 guest 请求并把它映射为 TaskStore 操作。"""

        server_version = "AgentGuestBridge/0.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {expected_token}")

        def send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self.authorized():
                audit({"instance": args.instance_id, "event": "auth_denied"})
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                if self.path == "/guest/tasks/next":
                    task = store.claim_next(args.instance_id)
                    if task is None:
                        # guest Worker 每秒短轮询一次。空队列轮询只是内部保活流量，
                        # 不能算作用户活动，否则 activity mtime 会被永久刷新，实例
                        # 永远无法达到空闲回收阈值。
                        self.send_response(204)
                        self.end_headers()
                        return
                    args.activity_file.touch()
                    audit({"instance": args.instance_id, "task": task["id"], "event": "claimed"})
                    self.send_json(200, {"id": task["id"], "prompt": task["prompt"]})
                    return

                match = TASK_EVENT_ROUTE.fullmatch(self.path)
                if match is None:
                    self.send_json(404, {"error": "route_not_found"})
                    return
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError:
                    self.send_json(400, {"error": "invalid_content_length"})
                    return
                if length < 1 or length > 1024 * 1024:
                    self.send_json(413, {"error": "event_too_large"})
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                    self.send_json(400, {"error": "invalid_event"})
                    return
                event_type = payload.pop("type")
                task_id = match.group(1)
                store.append_event(args.instance_id, task_id, event_type, payload)
                args.activity_file.touch()
                audit({"instance": args.instance_id, "task": task_id, "event": event_type})
                self.send_json(202, {"accepted": True})
            except (json.JSONDecodeError, TaskStoreError) as error:
                self.send_json(409, {"error": str(error)})
            except OSError as error:
                self.send_json(500, {"error": f"bridge_io_error: {error}"})

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), BridgeHandler)
    print(
        f"AGENT_BRIDGE_READY instance={args.instance_id} bind={args.listen_host}:{args.listen_port}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
