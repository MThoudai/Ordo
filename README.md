# Ordo

Ordo 是面向团队协作平台的多 Agent 智能支持助手，提供功能解释、页面操作指导、成员权限分析和技术问题排查。

## 功能

| 角色 | 职责 |
| --- | --- |
| General | 平台用途介绍、问题澄清与对话衔接 |
| Collaboration | 项目创建、成员邀请、文档协作、分享和权限规则 |
| Technical | 页面加载、文件传输、保存同步、站内提醒与固定 API 排查 |

系统支持主辅角色协作、知识检索、按用户与会话隔离的记忆、请求幂等、工具调用轨迹、运行监控，以及按需发起的意图和问答评测。人工支持请求会整理为沟通摘要。

Ordo 基于产品资料解释规则和提供指导。项目中没有真实协作平台业务后台；实时身份查询、权限修改、文档恢复、接口代执行、工单提交及人工接通暂不提供。

## 技术组成

- 后端：Python 3.12、FastAPI、Anthropic Python SDK。
- 前端：Vue 3、Vite、Nginx。
- 存储：Redis 保存会话与请求回执；ChromaDB 保存产品知识和长期记忆。
- 检索：本地 `all-MiniLM-L6-v2` 嵌入、查询改写与结果重排。
- 扩展：标准 MCP 工具与资源接口；外部 MCP 默认关闭。
- 监控：Prometheus 指标及运行状态展示。

模型服务需提供 Anthropic Messages 兼容接口，并支持工具调用。配置中的 API Key、模型名和服务地址应对应同一供应商。知识嵌入在本地生成，不需要单独的 Embedding API Key。

## 快速启动

准备 Docker Desktop 或 Docker Engine，并安装 Docker Compose v2。首次构建需要下载镜像、依赖及嵌入模型。

1. 复制配置示例：

   ```powershell
   Copy-Item backend/.env.example backend/.env
   ```

   Linux/macOS：`cp backend/.env.example backend/.env`。

2. 编辑 `backend/.env`，设置以下内容：

   ```dotenv
   ANTHROPIC_API_KEY=your-api-key
   ANTHROPIC_BASE_URL=https://your-messages-compatible-endpoint
   ANTHROPIC_MODEL=your-tool-capable-model
   REDIS_PASSWORD=your-local-redis-password
   ```

3. 在仓库根目录启动：

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./compose.ps1 up --detach --build
   ```

   Linux/macOS：`bash ./compose.sh up --detach --build`。

| 入口 | 默认地址 |
| --- | --- |
| Web 界面 | http://localhost:5175 |
| 后端 API 文档 | http://localhost:18000/docs |
| 健康检查 | http://localhost:18000/health |
| Prometheus | http://localhost:19090 |

端口可通过 `ORDO_FRONTEND_PORT`、`ORDO_BACKEND_PORT`、`ORDO_METRICS_PORT` 调整。默认仅绑定本机地址。

首次启动会从 `backend/knowledge/saas/manifest.json` 导入八份产品知识，在空数据卷中自动切块、嵌入并存储。仓库不附带预先生成的数据库、会话或评测结果。

## 使用

- **对话**：描述目标或粘贴脱敏提示，查看回答、主辅支持角色和资料来源。
- **知识库**：直接检索默认不调用模型；勾选增强检索会使用模型进行改写和重排。
- **评测**：界面会明确提交四条独立样例。启动评测会调用问答模型及评分模型，产生相应费用；API 空请求不会自动运行默认题库。

一次对话可能包含意图识别、多个 Agent 回合、改写、重排与合成，底层模型调用数可能高于消息数。不要提交密码、完整密钥、Cookie 或涉密文档。

## 配置与开发

运行代码位于 `backend/`，界面位于 `frontend/`。知识文档和 Agent Skill 是运行资源，分别位于 `backend/knowledge/saas/` 和 `backend/skills/`。

修改 Python 后重建后端；修改前端后重建前端：

```powershell
powershell -ExecutionPolicy Bypass -File ./compose.ps1 up --detach --build backend frontend
```

本地前端开发需要 Node.js 22：在 `frontend/` 执行 `npm ci`、`npm run dev`，访问 http://localhost:5176。默认代理到本机18000端口，其他后端地址可设置 `VITE_PYTHON_API_URL`。

配置示例列出了模型超时、短任务思考模式、可选意图嵌入和外部 MCP 等设置。`ORDO_API_TOKENS` 可配置接口访问身份，接口角色 `user/admin` 与知识中描述的平台角色不同；`ORDO_ENV=production` 时必须配置有效 Token 映射。

停止服务：

```powershell
powershell -ExecutionPolicy Bypass -File ./compose.ps1 down
```

普通停止保留数据卷。只有明确需要重置所有运行数据时才使用 `down --volumes`。
