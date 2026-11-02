# 项目文档

## 规划与设计

| 文档 | 内容 |
|---|---|
| [企划书](./企划书.md) | 平台目标、总体架构、多租户、工具与沙箱方案 |
| [架构与实现方案汇报](./架构与实现方案汇报.md) | Firecracker 实现、安全设计、存储、生命周期与验收方案 |
| [服务器单机 MVP 架构设计图](./服务器单机MVP架构设计图.md) | 单机部署拓扑、目录布局、启动流程与实施顺序 |
| [Pi 常驻进程与 Session 加载优化](./Pi常驻进程与Session加载优化.md) | Pi RPC 常驻可行性、官方协议能力调研与改造方案 |

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

仓库根目录的 `README.md` 统一记录 macOS/UTM 与 Windows/VMware 环境搭建、可执行脚本和当前实现；本目录保存规划与设计材料，其中尚未落地的模块不代表当前代码已经支持。
