#!/usr/bin/env python3
"""Agent 运行时的统一抽象层。

控制面通过该接口与「当前核心 Agent 运行时」交互，不直接依赖某个具体 Agent
（当前是 Pi）。Pi 的 JSONL 协议、``agent_settled`` 等细节被封装在
:class:`PiRuntimeAdapter` 内部，控制面只看到 ``execute`` / ``get_messages``
这两个稳定动作。

本模块只做「接口 + 适配」，不实现具体协议。将来接入其他 Agent 时，只需新增一个
:class:`AgentRuntimeAdapter` 的实现，控制面、SQLite 和 CLI 无需改动。

说明：``health_check`` / ``delete_session`` 暂未纳入接口——前者需要先明确「对
一个按连接启动的运行时，健康到底指什么」；后者当前是数据盘文件操作、由
InstanceRuntime 负责，边界尚需厘清，后续步骤再决定归属。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PI_GATEWAY_DIR = REPO_ROOT / "services" / "pi-gateway"
sys.path.insert(0, str(PI_GATEWAY_DIR))

from gateway import PiGateway, PiGatewayError  # noqa: E402


TaskEventHandler = Callable[[str, dict[str, Any]], None]


class AgentRuntimeError(RuntimeError):
    """Agent 运行时执行失败或不可达，可被控制面安全转换成错误响应。"""


class AgentRuntimeAdapter:
    """控制面与核心 Agent 运行时交互的最小稳定接口。

    当前只包含控制面真正依赖的两个动作，接口形状刻意保持最小，避免为尚未出现的
    第二种 Agent 做过度设计。
    """

    def execute(
        self,
        host: str,
        port: int,
        prompt: str,
        on_event: TaskEventHandler,
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """执行一个 prompt 并返回最终文本，流式事件经 on_event 上报。"""
        raise NotImplementedError

    def get_messages(
        self,
        host: str,
        port: int,
        workspace_id: str | None,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """读取当前会话的历史消息。"""
        raise NotImplementedError


class PiRuntimeAdapter(AgentRuntimeAdapter):
    """把现有 PiGateway 包装成平台统一的 Agent 运行时接口。

    Pi 特有的 JSONL 记录、``agent_settled`` 判定等全部留在 PiGateway 内部；
    这里只把 PiGateway 的异常统一映射为 :class:`AgentRuntimeError`。
    """

    def __init__(self, connect_timeout: float = 10, task_timeout: float = 240) -> None:
        self._gateway = PiGateway(connect_timeout, task_timeout)

    def execute(
        self,
        host: str,
        port: int,
        prompt: str,
        on_event: TaskEventHandler,
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        try:
            return self._gateway.execute(
                host, port, prompt, on_event, workspace_id, session_id
            )
        except PiGatewayError as error:
            raise AgentRuntimeError(str(error)) from error

    def get_messages(
        self,
        host: str,
        port: int,
        workspace_id: str | None,
        session_id: str,
    ) -> list[dict[str, Any]]:
        try:
            return self._gateway.get_messages(host, port, workspace_id, session_id)
        except PiGatewayError as error:
            raise AgentRuntimeError(str(error)) from error
