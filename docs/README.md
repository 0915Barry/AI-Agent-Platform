# 项目文档

## 规划与设计

| 文档 | 内容 |
|---|---|
| [企划书](./企划书.md) | 当前单机 MVP 的目标、对象模型、Runtime 边界和实现范围 |
| [架构与实现方案汇报](./架构与实现方案汇报.md) | 当前已实现模块、完整运行链路、存储、安全、生命周期与验收 |
| [服务器单机 MVP 架构设计图](./服务器单机MVP架构设计图.md) | Linux Host 的真实拓扑、目录、启动、通信和网络流程 |
| [Pi 常驻进程与 Session 加载优化](./Pi常驻进程与Session加载优化.md) | Pi RPC 常驻可行性、官方协议能力调研与改造方案 |

## 实施计划（尚未落地）

| 文档 | 内容 |
|---|---|
| [microVM 预热池实施计划](./microVM预热池实施计划.md) | 空白 Guest 预热、用户盘延迟绑定、运行身份映射、并发领取、快照协同及分阶段验收 |

## 问题排查记录

| 文档 | 内容 |
|---|---|
| [问题修复日志](./问题修复日志.md) | 按日期记录故障现象、影响、根因、修复方案、验证方式和当前状态 |
| [Pi 会话异步落盘竞态问题报告](./Pi会话异步落盘竞态问题报告.md) | Pi 原生 Session 偶发空文件的根因、定位过程与 `socat -t 30` 修复验证 |

## API 接口（供上中层前后端对接）

位于 [`api/`](./api/) 子目录：

| 文件 | 内容 |
|---|---|
| [openapi.yaml](./api/openapi.yaml) | 控制面 API 的机器可读 OpenAPI 3.0 规范，可生成客户端 SDK |
| [API契约.md](./api/API契约.md) | 给人看的接口契约（含 SSE 事件、错误码、聊天时序） |
| [api-preview.html](./api/api-preview.html) | Swagger UI 本地预览页 |
| [api-redoc.html](./api/api-redoc.html) | Redoc 本地预览页 |
| [协作约定.md](./api/协作约定.md) | 上下层联调接入与接口变更约定 |
| [联调接入测试.md](./api/联调接入测试.md) | 局域网接入方式介绍与验证步骤命令 |

本地预览（无需启动控制面或 microVM）：

```bash
./scripts/api-docs/render-api-docs.sh           # 启动（等于 start），自动打开浏览器
./scripts/api-docs/render-api-docs.sh status    # 查看运行状态
./scripts/api-docs/render-api-docs.sh stop      # 停止
./scripts/api-docs/render-api-docs.sh restart   # 重启
```

仓库根目录的 `README.md` 统一记录环境搭建和命令用法；本目录说明当前架构、实现边界
和上层接口。除明确标注为“调研”或“未实现”的内容外，架构文档应以当前仓库代码为准。
