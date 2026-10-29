#!/usr/bin/env python3
"""调用 Control Plane 的轻量终端客户端。

CLI 只理解平台 HTTP、SSE 和登录会话，不直接操作 Firecracker、SQLite 或 Pi RPC。
因此未来网页和桌面端可以复用完全相同的后端接口。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import select
import stat
import sys
from contextlib import contextmanager
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterator, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:18090"
DEFAULT_TIMEOUT = 30.0
STREAM_TIMEOUT = 300.0


class CliError(RuntimeError):
    """可直接展示给终端用户的错误。"""


class Console:
    """集中处理颜色和不同类型的终端消息。"""

    COLORS = {
        # 系统信息和错误在黑色终端上使用亮色变体，避免深蓝/深红
        # 与背景对比度过低。94/91 是标准 ANSI 亮蓝和亮红。
        "blue": "\033[94m",
        "cyan": "\033[36m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[91m",
    }
    RESET = "\033[0m"

    def __init__(self, enabled: bool, output: TextIO = sys.stdout) -> None:
        self.enabled = enabled
        self.output = output

    def paint(self, text: str, color: str) -> str:
        if not self.enabled:
            return text
        return f"{self.COLORS[color]}{text}{self.RESET}"

    def system(self, message: str) -> None:
        print(self.paint(f"[系统] {message}", "blue"), file=self.output)

    def success(self, message: str) -> None:
        print(self.paint(f"[成功] {message}", "green"), file=self.output)

    def tool(self, message: str) -> None:
        print(self.paint(f"[工具] {message}", "yellow"), file=self.output)

    def warning(self, message: str) -> None:
        print(self.paint(f"[警告] {message}", "yellow"), file=self.output)

    def error(self, message: str) -> None:
        print(self.paint(f"[错误] {message}", "red"), file=sys.stderr)


class SessionStore:
    """以 0600 权限保存 Control Plane 会话 Cookie。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CliError(f"无法读取登录状态：{error}") from error
        if not isinstance(value, dict):
            raise CliError("登录状态文件格式错误，请重新登录")
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            temporary.replace(self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as error:
            raise CliError(f"无法保存登录状态：{error}") from error

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise CliError(f"无法删除登录状态：{error}") from error


class ApiClient:
    """Control Plane HTTP/SSE 客户端。"""

    def __init__(self, base_url: str, cookie: str | None = None) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CliError("Control Plane 地址必须是有效的 http:// 或 https:// URL")
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Request:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return Request(self.base_url + path, data=data, headers=headers, method=method)

    @staticmethod
    def _api_error(error: HTTPError) -> CliError:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            detail = payload.get("error", {})
            message = detail.get("message") or error.reason
            code = detail.get("code")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = error.reason
            code = None
        prefix = f"HTTP {error.code}"
        if code:
            prefix += f"/{code}"
        return CliError(f"{prefix}：{message}")

    def json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], Any]:
        try:
            response = urlopen(self._request(method, path, payload), timeout=DEFAULT_TIMEOUT)
            with response:
                body = json.loads(response.read().decode("utf-8"))
                return body, response.headers
        except HTTPError as error:
            raise self._api_error(error) from error
        except URLError as error:
            raise CliError(f"无法连接 Control Plane：{error.reason}") from error
        except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CliError(f"Control Plane 响应异常：{error}") from error

    def events(self, path: str) -> Iterator[dict[str, Any]]:
        request = self._request("GET", path)
        request.add_header("Accept", "text/event-stream")
        try:
            response = urlopen(request, timeout=STREAM_TIMEOUT)
        except HTTPError as error:
            raise self._api_error(error) from error
        except URLError as error:
            raise CliError(f"无法连接任务事件流：{error.reason}") from error

        data_lines: list[str] = []
        try:
            with response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            try:
                                event = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError as error:
                                raise CliError("任务事件流包含无效 JSON") from error
                            if isinstance(event, dict):
                                yield event
                            data_lines.clear()
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except (TimeoutError, UnicodeDecodeError) as error:
            raise CliError(f"任务事件流中断：{error}") from error


def default_state_path() -> Path:
    config_root = os.environ.get("XDG_CONFIG_HOME")
    if config_root:
        return Path(config_root) / "ai-agent-platform" / "agentctl-session.json"
    return Path.home() / ".config" / "ai-agent-platform" / "agentctl-session.json"


def extract_cookie(headers: Any) -> str:
    raw_cookie = headers.get("Set-Cookie")
    if not raw_cookie:
        raise CliError("登录成功响应中缺少会话 Cookie")
    cookie = SimpleCookie(raw_cookie)
    morsel = cookie.get("aap_session")
    if morsel is None or not morsel.value:
        raise CliError("登录成功响应中的会话 Cookie 无效")
    return f"aap_session={morsel.value}"


def require_cookie(store: SessionStore, base_url: str) -> tuple[str, dict[str, Any]]:
    state = store.load()
    if state.get("baseUrl") != base_url or not isinstance(state.get("cookie"), str):
        raise CliError("尚未登录当前 Control Plane，请先运行 ./agentctl login")
    return state["cookie"], state


def command_login(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    username = args.username or input("用户名：").strip()
    if not username:
        raise CliError("用户名不能为空")
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise CliError("标准输入中没有密码")
    else:
        password = getpass.getpass("密码：")
    client = ApiClient(args.base_url)
    payload, headers = client.json(
        "POST", "/api/auth/login", {"username": username, "password": password}
    )
    cookie = extract_cookie(headers)
    store.save(
        {
            "baseUrl": args.base_url,
            "cookie": cookie,
            "username": payload["user"]["username"],
            "expiresAt": payload["expiresAt"],
        }
    )
    console.success(f"已登录：{payload['user']['username']}")


def command_logout(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    cookie, _ = require_cookie(store, args.base_url)
    ApiClient(args.base_url, cookie).json("POST", "/api/auth/logout", {})
    store.clear()
    console.success("已退出登录")


def runtime_summary(instance: dict[str, Any]) -> tuple[str, str, str]:
    runtime = instance.get("runtime")
    if not isinstance(runtime, dict):
        return "-", "-", "-"
    guest = str(runtime.get("guestIp") or "-")
    port = str(runtime.get("rpcPort") or "-")
    idle = runtime.get("idleSeconds")
    return guest, port, "-" if idle is None else f"{idle}s"


def print_instance(instance: dict[str, Any], console: Console) -> None:
    guest, port, idle = runtime_summary(instance)
    console.system(
        f"实例={instance['id']} 状态={instance['status']} Guest={guest}:{port} 空闲={idle}"
    )


def display_time(value: Any) -> str:
    if not isinstance(value, int):
        return "未知时间"
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def display_menu_time(value: Any) -> str:
    """菜单统一使用固定的月/日和24小时制，不使用“今天/昨天”。"""

    if not isinstance(value, int):
        return "--/-- --:--"
    return datetime.fromtimestamp(value).astimezone().strftime("%m/%d %H:%M")


def one_line(value: Any, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def menu_preview(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "暂无消息"
    return text if len(text) <= 10 else text[:10] + "…"


@contextmanager
def raw_terminal() -> Iterator[None]:
    """临时启用逐键读取；退出时始终恢复原终端设置。"""

    if os.name == "nt":
        yield
        return
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_menu_key() -> str:
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "other")
        return {"\r": "enter", "\x1b": "escape"}.get(key, "other")

    key = os.read(sys.stdin.fileno(), 1)
    if key in {b"\r", b"\n"}:
        return "enter"
    if key != b"\x1b":
        return "other"
    if not select.select([sys.stdin], [], [], 0.05)[0]:
        return "escape"
    sequence = os.read(sys.stdin.fileno(), 2)
    return {b"[A": "up", b"[B": "down"}.get(sequence, "other")


def choose_option(title: str, labels: list[str], initial: int = 0) -> int | None:
    """显示轻量方向键菜单，返回选中项序号；Esc返回None。"""

    if not labels:
        raise CliError("没有可选择的项目")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise CliError("交互选择需要终端；自动化调用请显式提供ID")
    selected = min(max(initial, 0), len(labels) - 1)
    print(title)
    print("\n" * len(labels), end="", flush=True)
    # 选择期间隐藏终端的白色输入光标，避免它停在菜单中间。
    # 不论 Enter、Esc 还是异常退出，finally 都会恢复光标。
    print("\033[?25l", end="", flush=True)
    try:
        with raw_terminal():
            while True:
                print(f"\033[{len(labels)}A", end="")
                for index, label in enumerate(labels):
                    marker = "❯" if index == selected else " "
                    print(f"\033[2K\r{marker} {label}")
                key = read_menu_key()
                if key == "up":
                    selected = (selected - 1) % len(labels)
                elif key == "down":
                    selected = (selected + 1) % len(labels)
                elif key == "enter":
                    return selected
                elif key == "escape":
                    return None
    finally:
        # raw mode 中换行只会向下移动，不一定回到第 1 列。显式输出
        # \r，确保后续的 [成功]、[系统] 从左侧开始。
        print("\033[?25h\r", end="", flush=True)


def print_message(message: dict[str, Any], console: Console) -> None:
    role = message.get("role")
    if role == "user":
        label = console.paint("你 > ", "cyan")
    elif role == "assistant":
        label = console.paint("Pi > ", "green")
    else:
        label = console.paint(f"{role or 'unknown'} > ", "yellow")
    print(
        console.paint(f"[{display_time(message.get('createdAt'))}] ", "blue")
        + label
        + str(message.get("content") or ""),
        file=console.output,
    )


def authenticated_client(args: argparse.Namespace, store: SessionStore) -> ApiClient:
    cookie, _ = require_cookie(store, args.base_url)
    return ApiClient(args.base_url, cookie)


def command_create(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    client = authenticated_client(args, store)
    payload = {} if args.instance_id is None else {"id": args.instance_id}
    instance, _ = client.json("POST", "/api/instances", payload)
    console.success(f"实例已创建：{instance['id']}")


def command_start(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    client = authenticated_client(args, store)
    console.system(f"正在启动实例 {args.instance_id}……")
    instance, _ = client.json("POST", f"/api/instances/{args.instance_id}/start", {})
    console.success("实例启动完成")
    print_instance(instance, console)


def command_status(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    client = authenticated_client(args, store)
    if args.instance_id:
        instance, _ = client.json("GET", f"/api/instances/{args.instance_id}")
        print_instance(instance, console)
        return
    payload, _ = client.json("GET", "/api/instances")
    instances = payload.get("instances", [])
    if not instances:
        console.warning("当前账户还没有实例")
        return
    for instance in instances:
        print_instance(instance, console)


def command_conversations(
    args: argparse.Namespace, store: SessionStore, console: Console
) -> None:
    client = authenticated_client(args, store)
    payload, _ = client.json("GET", f"/api/instances/{args.instance_id}/conversations")
    conversations = payload.get("conversations", [])
    if not conversations:
        console.warning("该实例还没有会话")
        return
    for conversation in conversations:
        print(
            f"{conversation['id']}  "
            f"{display_time(conversation.get('updatedAt'))}  "
            f"{one_line(conversation.get('title'), 32)}",
            file=console.output,
        )
        preview = one_line(conversation.get("preview"))
        print(console.paint(f"  {preview}", "blue"), file=console.output)


def command_workspace(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    """创建或选择当前用户的工作区。"""

    client = authenticated_client(args, store)
    if args.new_name is not None:
        workspace, _ = client.json("POST", "/api/workspaces", {"name": args.new_name})
        console.success(f"当前工作区：{workspace['name']}")
        return
    if args.select_id is not None:
        workspace, _ = client.json(
            "POST", f"/api/workspaces/{args.select_id}/select", {}
        )
        console.success(f"当前工作区：{workspace['name']}")
        return

    payload, _ = client.json("GET", "/api/workspaces")
    workspaces = payload.get("workspaces", [])
    labels = [console.paint("＋ New workspace", "green")]
    initial = 0
    for index, workspace in enumerate(workspaces, start=1):
        count = int(workspace.get("conversationCount") or 0)
        current = console.paint(" · 当前", "green") if workspace.get("current") else ""
        labels.append(
            f"{console.paint(workspace['name'], 'cyan')}  "
            f"{console.paint(f'{count}个会话', 'yellow')} · "
            f"{console.paint(display_menu_time(workspace.get('updatedAt')), 'blue')}"
            f"{current}"
        )
        if workspace.get("current"):
            initial = index
    selected = choose_option("请选择工作区：", labels, initial)
    if selected is None:
        return
    if selected == 0:
        name = input("工作区名称：").strip()
        if not name:
            raise CliError("工作区名称不能为空")
        workspace, _ = client.json("POST", "/api/workspaces", {"name": name})
    else:
        selected_workspace = workspaces[selected - 1]
        action = choose_option(
            f"工作区：{selected_workspace['name']}",
            [
                console.paint("打开", "green"),
                console.paint("删除", "red"),
                "取消",
            ],
        )
        if action is None or action == 2:
            return
        workspace_id = selected_workspace["id"]
        if action == 1:
            confirmation = input(
                f"删除工作区会同时删除其会话和工作文件。\n"
                f"请输入工作区名称 {selected_workspace['name']} 确认："
            ).strip()
            if confirmation != selected_workspace["name"]:
                raise CliError("工作区名称不匹配，已取消删除")
            result, _ = client.json("DELETE", f"/api/workspaces/{workspace_id}")
            current = result.get("currentWorkspace") or {}
            console.success(
                f"工作区已删除：{selected_workspace['name']}；"
                f"当前工作区：{current.get('name') or '-'}"
            )
            return
        workspace, _ = client.json("POST", f"/api/workspaces/{workspace_id}/select", {})
    console.success(f"当前工作区：{workspace['name']}")


def fetch_history(
    client: ApiClient,
    instance_id: str,
    conversation_id: str,
    limit: int,
    before: int | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"limit": limit}
    if before is not None:
        query["before"] = before
    path = (
        f"/api/instances/{instance_id}/conversations/{conversation_id}/messages?"
        + urlencode(query)
    )
    payload, _ = client.json("GET", path)
    return payload


def render_history(payload: dict[str, Any], console: Console) -> None:
    messages = payload.get("messages", [])
    if not messages:
        console.warning("该页没有消息")
    for message in messages:
        print_message(message, console)
    next_before = payload.get("nextBefore")
    if isinstance(next_before, int):
        console.system(f"还有更早消息，可添加 --before {next_before} 继续查看")


def command_history(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    client = authenticated_client(args, store)
    payload = fetch_history(
        client, args.instance_id, args.conversation_id, args.limit, args.before
    )
    render_history(payload, console)


def command_stop(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    client = authenticated_client(args, store)
    console.system(f"正在停止实例 {args.instance_id}……")
    instance, _ = client.json("POST", f"/api/instances/{args.instance_id}/stop", {})
    console.success(f"实例已停止，数据盘保留：{instance['id']}")


def command_destroy(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    if not args.yes:
        if not sys.stdin.isatty():
            raise CliError("非交互环境销毁实例时必须添加 --yes")
        answer = input(f"此操作会删除实例记录和关联会话。请输入实例 ID {args.instance_id} 确认：")
        if answer.strip() != args.instance_id:
            raise CliError("实例 ID 不匹配，已取消销毁")
    client = authenticated_client(args, store)
    result, _ = client.json("DELETE", f"/api/instances/{args.instance_id}")
    console.success(f"实例已销毁：{result['id']}")


def render_task_events(
    client: ApiClient, instance_id: str, task_id: str, console: Console
) -> None:
    text_started = False
    for event in client.events(f"/api/instances/{instance_id}/tasks/{task_id}/stream"):
        event_type = event.get("type")
        payload = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "progress":
            if text_started:
                print(file=console.output)
                text_started = False
            stage = payload.get("stage", "处理中")
            console.system(f"任务状态：{stage}")
        elif event_type == "text":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                if not text_started:
                    print(console.paint("Pi > ", "green"), end="", file=console.output, flush=True)
                    text_started = True
                print(delta, end="", file=console.output, flush=True)
        elif event_type == "tool_call":
            if text_started:
                print(file=console.output)
                text_started = False
            console.tool(f"调用 {payload.get('toolName') or 'unknown'}")
        elif event_type == "tool_result":
            if text_started:
                print(file=console.output)
                text_started = False
            state = "失败" if payload.get("isError") else "完成"
            console.tool(f"{payload.get('toolName') or 'unknown'} {state}")
        elif event_type == "failed":
            if text_started:
                print(file=console.output)
            raise CliError(str(payload.get("error") or "Agent 任务失败"))
        elif event_type == "completed":
            if text_started:
                print(file=console.output)
            elif isinstance(payload.get("output"), str):
                print(console.paint("Pi > ", "green") + payload["output"], file=console.output)
            return
    raise CliError("任务事件流在返回最终状态前中断")


def command_chat(args: argparse.Namespace, store: SessionStore, console: Console) -> None:
    client = authenticated_client(args, store)
    conversation_id = args.conversation
    instance_id = args.instance_id
    created_new = False

    if instance_id is None:
        workspace_payload, _ = client.json("GET", "/api/workspaces")
        workspaces = workspace_payload.get("workspaces", [])
        if args.workspace is not None:
            workspace = next(
                (item for item in workspaces if item.get("id") == args.workspace), None
            )
            if workspace is None:
                raise CliError(f"工作区不存在：{args.workspace}")
            client.json("POST", f"/api/workspaces/{args.workspace}/select", {})
        else:
            workspace = next((item for item in workspaces if item.get("current")), None)
        if workspace is None:
            raise CliError("当前没有工作区，请先运行 ./agentctl workspace")

        payload, _ = client.json(
            "GET", f"/api/workspaces/{workspace['id']}/conversations"
        )
        conversations = payload.get("conversations", [])
        if conversation_id is None:
            labels = [console.paint("＋ New conversation", "green")]
            labels.extend(
                f"{console.paint(item.get('title') or '新会话', 'cyan')}  "
                f"{console.paint(display_menu_time(item.get('updatedAt')), 'blue')} · "
                f"{console.paint(menu_preview(item.get('preview')), 'yellow')}"
                for item in conversations[:20]
            )
            selected = choose_option(f"{workspace['name']} / 请选择会话：", labels)
            if selected is None:
                return
            if selected == 0:
                conversation, _ = client.json(
                    "POST", f"/api/workspaces/{workspace['id']}/conversations", {}
                )
                created_new = True
            else:
                conversation = conversations[selected - 1]
                action = choose_option(
                    f"会话：{conversation.get('title') or '新会话'}",
                    [
                        console.paint("打开", "green"),
                        console.paint("删除", "red"),
                        "取消",
                    ],
                )
                if action is None or action == 2:
                    return
                if action == 1:
                    confirmation = input("确认删除该会话？请输入 y：").strip().lower()
                    if confirmation != "y":
                        raise CliError("已取消删除")
                    client.json(
                        "DELETE",
                        f"/api/workspaces/{workspace['id']}/conversations/{conversation['id']}",
                    )
                    console.success(f"会话已删除：{conversation.get('title') or conversation['id']}")
                    return
                conversation_id = conversation["id"]
        else:
            conversation = next(
                (item for item in conversations if item.get("id") == conversation_id), None
            )
            if conversation is None:
                raise CliError(f"会话不属于当前工作区：{conversation_id}")
        conversation_id = conversation["id"]
        instance_id = conversation["instanceId"]

    if conversation_id is None:
        conversation, _ = client.json(
            "POST", f"/api/instances/{instance_id}/conversations", {"title": "新会话"}
        )
        conversation_id = conversation["id"]
        created_new = True
    if not created_new:
        conversation, _ = client.json(
            "GET", f"/api/instances/{instance_id}/conversations/{conversation_id}"
        )
        console.system(f"继续会话：{conversation.get('title') or conversation_id}")
        render_history(
            fetch_history(client, instance_id, conversation_id, args.history_limit),
            console,
        )
    console.system(f"会话={conversation_id}；输入 /exit 退出，不会停止实例")

    while True:
        try:
            message = input(console.paint("你 > ", "cyan"))
        except EOFError:
            print()
            return
        if message.strip() in {"/exit", "/quit"}:
            return
        if not message.strip():
            continue
        task, _ = client.json(
            "POST",
            f"/api/instances/{instance_id}/conversations/{conversation_id}/messages",
            {"content": message},
        )
        render_task_events(client, instance_id, task["id"], console)


def message_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if parsed < 1 or parsed > 100:
        raise argparse.ArgumentTypeError("必须在 1 到 100 之间")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description=(
            "AI Agent Platform 终端客户端。\n"
            "日常使用只需选择工作区和会话；实例命令保留用于诊断。"
        ),
        epilog=(
            "常用流程：\n"
            "  ./agentctl login <用户名>\n"
            "  ./agentctl workspace\n"
            "  ./agentctl chat\n\n"
            "诊断实例：\n"
            "  ./agentctl status\n"
            "  ./agentctl stop <实例ID>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AAP_BASE_URL", DEFAULT_BASE_URL),
        help=f"Control Plane 地址（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    parser.add_argument("--no-color", action="store_true", help="关闭 ANSI 颜色")
    commands = parser.add_subparsers(
        dest="command", required=True, title="命令", metavar="COMMAND"
    )

    login = commands.add_parser(
        "login",
        help="登录平台：login [USERNAME]",
        description="登录 Control Plane，并以 0600 权限在本机保存短期会话 Cookie。",
        epilog="示例：./agentctl login barry-dev",
    )
    login.add_argument("username", nargs="?", metavar="USERNAME", help="平台用户名；省略时交互输入")
    login.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读取一行密码，仅供自动化测试使用",
    )
    commands.add_parser(
        "logout",
        help="退出平台：logout",
        description="撤销服务端会话并删除本机保存的 Cookie。",
    )

    workspace = commands.add_parser(
        "workspace",
        help="创建或选择工作区：workspace",
        description="使用方向键创建或切换当前工作区。",
        epilog=(
            "示例：./agentctl workspace\n"
            "      ./agentctl workspace --new 'AI Agent Platform'\n"
            "      ./agentctl workspace --select workspace-1234"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    workspace_actions = workspace.add_mutually_exclusive_group()
    workspace_actions.add_argument("--new", dest="new_name", metavar="NAME")
    workspace_actions.add_argument("--select", dest="select_id", metavar="WORKSPACE_ID")

    create = commands.add_parser(
        "create",
        help="创建实例记录：create [INSTANCE_ID]",
        description="创建属于当前用户的逻辑实例；此时不会启动 microVM。",
        epilog="示例：./agentctl create cli-demo",
    )
    create.add_argument(
        "instance_id", nargs="?", metavar="INSTANCE_ID", help="实例 ID；省略时由平台自动生成"
    )
    start = commands.add_parser(
        "start",
        help="启动实例：start INSTANCE_ID",
        description="为指定实例准备数据盘和网络，并启动 Firecracker microVM。",
        epilog="示例：./agentctl start cli-demo",
    )
    start.add_argument("instance_id", metavar="INSTANCE_ID", help="要启动的实例 ID")
    status_parser = commands.add_parser(
        "status",
        help="查看状态：status [INSTANCE_ID]",
        description="查看指定实例；省略 INSTANCE_ID 时列出当前用户的全部实例。",
        epilog="示例：./agentctl status cli-demo\n      ./agentctl status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status_parser.add_argument(
        "instance_id", nargs="?", metavar="INSTANCE_ID", help="可选的实例 ID"
    )
    conversations = commands.add_parser(
        "conversations",
        help="列出会话：conversations INSTANCE_ID",
        description="列出指定实例的会话 ID、标题、更新时间和消息预览。",
        epilog="示例：./agentctl conversations cli-demo",
    )
    conversations.add_argument("instance_id", metavar="INSTANCE_ID", help="实例 ID")
    history = commands.add_parser(
        "history",
        help="查看历史：history INSTANCE_ID CONVERSATION_ID",
        description="分页查看指定实例、指定会话的历史消息。",
        epilog=(
            "示例：./agentctl history cli-demo conversation-1234\n"
            "      ./agentctl history cli-demo conversation-1234 --limit 50 --before 120"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    history.add_argument("instance_id", metavar="INSTANCE_ID", help="实例 ID")
    history.add_argument("conversation_id", metavar="CONVERSATION_ID", help="会话 ID")
    history.add_argument(
        "--limit", type=message_limit, default=20, metavar="COUNT", help="本页消息数，1-100（默认 20）"
    )
    history.add_argument("--before", type=int, metavar="SEQUENCE", help="读取该消息序号之前的一页")
    chat = commands.add_parser(
        "chat",
        help="选择并进入会话：chat [LEGACY_INSTANCE_ID]",
        description="从当前工作区选择会话；后台会自动准备用户的 microVM。",
        epilog=(
            "示例：./agentctl chat\n"
            "      ./agentctl chat --conversation conversation-1234\n"
            "兼容旧用法：./agentctl chat cli-demo\n"
            "对话中输入 /exit 或 /quit 返回终端，不会停止实例。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    chat.add_argument(
        "instance_id",
        nargs="?",
        metavar="LEGACY_INSTANCE_ID",
        help="兼容旧接口的实例 ID；通常无需提供",
    )
    chat.add_argument("--workspace", metavar="WORKSPACE_ID", help="直接选择工作区")
    chat.add_argument("--conversation", metavar="CONVERSATION_ID", help="继续已有会话 ID")
    chat.add_argument(
        "--history-limit",
        type=message_limit,
        default=10,
        help="继续会话时展示的最近消息数（默认 10）",
    )
    stop = commands.add_parser(
        "stop",
        help="停止实例：stop INSTANCE_ID",
        description="停止 microVM 并释放临时网络资源，但保留数据盘、任务和会话。",
        epilog="示例：./agentctl stop cli-demo",
    )
    stop.add_argument("instance_id", metavar="INSTANCE_ID", help="要停止的实例 ID")
    destroy = commands.add_parser(
        "destroy",
        help="销毁实例：destroy INSTANCE_ID",
        description="删除旧式实例记录及其关联任务和会话；用户持久盘保留。",
        epilog="示例：./agentctl destroy cli-demo",
    )
    destroy.add_argument("instance_id", metavar="INSTANCE_ID", help="要永久销毁的实例 ID")
    destroy.add_argument("--yes", action="store_true", help="跳过实例 ID 二次确认")
    return parser


COMMANDS = {
    "login": command_login,
    "logout": command_logout,
    "workspace": command_workspace,
    "create": command_create,
    "start": command_start,
    "status": command_status,
    "conversations": command_conversations,
    "history": command_history,
    "chat": command_chat,
    "stop": command_stop,
    "destroy": command_destroy,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    colors_enabled = not args.no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()
    console = Console(colors_enabled)
    store = SessionStore(args.state_file)
    try:
        COMMANDS[args.command](args, store, console)
        return 0
    except KeyboardInterrupt:
        print()
        console.warning("操作已取消")
        return 130
    except CliError as error:
        console.error(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
