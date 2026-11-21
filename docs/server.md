# 服务与命令行

## HTTP 服务

需要 `pip install "No-Agent[server]"`。

```python
# 挂到已有的 FastAPI 应用
from no_agent.server import create_router
app.include_router(create_router(agent), prefix="/agent")
```

```bash
# 独立运行（从 NO_AGENT_* 环境变量读配置，见 .env.example）
uvicorn no_agent.server:app
docker compose up                  # 见 docker-compose.yml，数据在 /data 卷中
```

### 鉴权

- **独立运行**（`uvicorn no_agent.server:app` / Docker / `create_app()`）：设置了 `NO_AGENT_API_TOKEN` 则所有 `/api/v1` 接口要求 `Authorization: Bearer <token>`；没有设置时只接受本机（回环地址）请求。**Docker 等对外暴露端口的部署必须设置 token**。`/health` 不需要鉴权。
- **挂到已有应用**：`create_router(agent)` 默认不做鉴权，由宿主应用负责（例如 `include_router(..., dependencies=[...])`）；也可以传 `create_router(agent, api_token=...)`。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/run` | 执行任务；`mode=freeform` 强制 AI 做，`mode=skill` + `skill_id` 指定技能 |
| GET | `/api/v1/runs`、`/api/v1/runs/{id}` | 运行记录与完整轨迹 |
| POST | `/api/v1/feedback` | 反馈 `{run_id, score: -1~1, comment}` |
| GET | `/api/v1/skills`、`/api/v1/skills/{id}` | 技能列表 / 详情（含类型、所在层、指南正文、脚本源码） |
| POST | `/api/v1/skills/{id}/run` | 显式调用技能 `{params}` 或 `{task}`，参数不合法返回 422 |
| POST | `/api/v1/skills/{id}/promote`、`/reject` | 手动让候选版本转正 / 淘汰 |
| POST | `/api/v1/skills/{id}/activate` | 切换 / 回滚版本 `{version}` |
| POST | `/api/v1/skills/{id}/retire`，DELETE `/api/v1/skills/{id}` | 下线（一并淘汰候选版本）/ 删除（只读层技能不能删） |
| GET | `/api/v1/reviews` | 等待审查的版本：原因 + diff |
| POST | `/api/v1/skills/{id}/versions/{v}/approve`、`/reject` | 批准 / 拒绝扩大了能力的版本 |
| GET | `/api/v1/evolver` | 后台进化任务队列与历史（含每次进化消耗的 token） |
| GET | `/api/v1/tools` | 可用工具 |

## 命令行

```bash
No-Agent mcp                 # 启动 MCP 服务（stdio），见 mcp.md
No-Agent serve --port 8000   # 启动 HTTP 服务
No-Agent skills              # 列出技能（含候选版本、成功率）
No-Agent install <目录>      # 安装技能
No-Agent reviews --diff      # 查看待审版本；approve / reject 批准或拒绝，见 security.md
No-Agent sync                # 拉取 / 更新 git 技能仓库
No-Agent keygen / sign       # 生成签名密钥 / 给技能签名
```

命令行和 HTTP 服务都通过 `AgentConfig.from_env()` 读取配置。
