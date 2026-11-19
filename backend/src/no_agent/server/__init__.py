"""
可选的 HTTP 服务（需要 pip install "No-Agent[server]"）

嵌入已有 FastAPI 应用：
    from no_agent.server import create_router
    app.include_router(create_router(agent), prefix="/agent")

独立运行（从环境变量 NO_AGENT_* 读取配置）：
    uvicorn no_agent.server:app
"""

from __future__ import annotations

from contextlib import asynccontextmanager

try:
    from fastapi import FastAPI
except ImportError as e:  # pragma: no cover
    raise ImportError('No-Agent 的 HTTP 服务需要额外依赖：pip install "No-Agent[server]"') from e

from no_agent import __version__
from no_agent.agent import Agent
from no_agent.config import AgentConfig
from no_agent.server.routes import create_router


def create_app(agent: Agent | None = None) -> FastAPI:
    holder: dict[str, Agent] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["agent"] = agent or Agent(AgentConfig.from_env())
        await holder["agent"].start()
        yield
        await holder["agent"].stop(wait=False)

    app = FastAPI(
        title="No-Agent",
        version=__version__,
        description="自进化 Agent：技能脚本优先，失败时 AI 接管并自动修补，未知任务由 AI 完成并提炼为新技能",
        lifespan=lifespan,
    )
    app.include_router(create_router(lambda: holder["agent"]), prefix="/api/v1")

    @app.get("/health")
    async def health_check() -> dict:
        return {"status": "ok", "version": __version__}

    return app


def __getattr__(name: str):
    # uvicorn no_agent.server:app —— 按需创建，避免 import 时就读取环境
    if name == "app":
        return create_app()
    raise AttributeError(name)


__all__ = ["create_app", "create_router"]
