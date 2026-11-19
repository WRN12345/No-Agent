"""
对外入口：Agent

    from no_agent import Agent, AgentConfig, LLMConfig, tool

    @tool("query_orders", description="按状态查询订单", parameters={...})
    async def query_orders(status: str): ...

    agent = Agent(AgentConfig(llm=LLMConfig(api_key="...")), tools=[query_orders])

    async with agent:                    # 启动后台进化器，退出时等待进化任务完成
        run = await agent.run("查一下所有待发货的订单")
        print(run.mode, run.result)

    # 或在同步代码中：
    run = agent.run_sync("查一下所有待发货的订单")
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from no_agent.config import AgentConfig
from no_agent.engine.agent import EvolvingAgent, RunMode
from no_agent.engine.evolver import Evolver
from no_agent.llm import LLMs, build_llms
from no_agent.skills.matcher import SkillMatcher
from no_agent.storage.base import RunStore, SkillStore
from no_agent.storage.files import FileSkillStore
from no_agent.storage.home import init_home, resolve_home
from no_agent.storage.sql import Run, SqlRunStore
from no_agent.tools.base import Tool, ToolRegistry
from no_agent.tools.builtin import builtin_tools

logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        tools: list[Tool] | None = None,
        include_builtin_tools: bool = True,
        llms: LLMs | None = None,
        skill_store: SkillStore | None = None,
        run_store: RunStore | None = None,
    ) -> None:
        self.config = cfg = config or AgentConfig()
        self._home: Path | None = None

        # ── 存储：只有用到默认实现时才创建 home 目录 ──
        self.store: SkillStore = skill_store or FileSkillStore(
            cfg.skills_dir or self.home / "skills",
            readonly_dirs=cfg.extra_skill_dirs,
            stats_dir=self.home / ".stats",
        )
        self.runs: RunStore = run_store or SqlRunStore(cfg.database_url or f"sqlite:///{(self.home / 'runs.db').as_posix()}")

        # ── 工具 ──
        self.registry = ToolRegistry()
        if include_builtin_tools:
            for t in builtin_tools(cfg.workspace_dir or self.home / "workspace"):
                self.registry.register(t)
        for t in tools or []:
            self.registry.register(t)

        # ── 引擎 ──
        self.llms = llms or build_llms(cfg.llm)
        self.matcher = SkillMatcher(
            self.store,
            self.llms.fast,
            embedder=self.llms.embedder,
            top_k=cfg.match_top_k,
            min_score=cfg.match_min_score,
            min_confidence=cfg.match_min_confidence,
        )
        self.evolver = Evolver(
            store=self.store, runs=self.runs, registry=self.registry, llms=self.llms, matcher=self.matcher, settings=cfg
        )
        self.engine = EvolvingAgent(
            store=self.store,
            matcher=self.matcher,
            registry=self.registry,
            llms=self.llms,
            runs=self.runs,
            evolver=self.evolver,
            settings=cfg,
        )

    @property
    def home(self) -> Path:
        """数据根目录（首次访问时解析并创建）"""
        if self._home is None:
            self._home = init_home(resolve_home(self.config.home))
            logger.info("No-Agent 数据目录: %s", self._home)
        return self._home

    def add_tool(self, t: Tool) -> None:
        self.registry.register(t)

    # ── 生命周期 ──

    async def start(self) -> None:
        """启动后台进化器（run() 会自动调用）"""
        self.evolver.start()

    async def stop(self, *, wait: bool = True) -> None:
        """停止后台进化器；wait=True 时先等待排队中的进化任务完成"""
        if wait:
            await self.evolver.drain()
        await self.evolver.stop()

    async def wait_idle(self) -> None:
        """等待所有进化任务完成"""
        await self.evolver.drain()

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # ── 执行 ──

    async def run(
        self,
        task: str,
        *,
        mode: RunMode = "auto",
        skill_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Run:
        """
        执行任务。mode:
          auto     自动选择：命中技能就跑脚本，否则 AI 自由执行
          freeform 强制 AI 自由执行
          skill    指定 skill_id（可同时给 params 跳过参数抽取）
        """
        self.evolver.start()
        return await self.engine.run(task, mode=mode, skill_id=skill_id, params=params)

    def run_sync(self, task: str, **kwargs: Any) -> Run:
        """同步执行（会等待本次触发的进化任务完成后返回）"""

        async def _main() -> Run:
            try:
                return await self.run(task, **kwargs)
            finally:
                await self.stop()

        return asyncio.run(_main())
