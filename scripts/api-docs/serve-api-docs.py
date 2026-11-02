#!/usr/bin/env python3
"""在本地预览 API 文档（Swagger UI / Redoc）的静态服务器。

Python 自带的 `python3 -m http.server` 会把 .yaml 当成 application/octet-stream
返回，导致 Swagger UI 无法识别。这个小脚本补上正确的 MIME 类型，其余行为一致，
仍只依赖 Python 标准库。
"""
from __future__ import annotations

import http.server
import os
import sys

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class DocsHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
    }


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    docs_api_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "api"
    )
    os.chdir(docs_api_dir)
    server = http.server.ThreadingHTTPServer((HOST, port), DocsHandler)
    print("API 文档已启动（按 Ctrl-C 停止）：")
    print(f"  Swagger UI：http://{HOST}:{port}/api-preview.html")
    print(f"  Redoc     ：http://{HOST}:{port}/api-redoc.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
