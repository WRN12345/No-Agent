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
import json
import logging
from pathlib import Path
from typing import Any

from no_agent.config import AgentConfig
from no_agent.interact import Asker, default_asker, make_ask_tool
from no_agent.engine.agent import EvolvingAgent, RunMode
from no_agent.engine.evolver import Evolver
from no_agent.llm import LLMs, build_llms
from no_agent.skills.matcher import SkillMatcher
from no_agent.skills.models import Skill
from no_agent.storage.base import RunStore, SkillStore
from no_agent.storage.files import FileSkillStore
from no_agent.storage.home import init_home, resolve_home
from no_agent.storage.sql import Run, SqlRunStore
from no_agent.tools.base import Tool, ToolRegistry
from no_agent.tools.builtin import builtin_tools

BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"
_DEFAULT = object()

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
        asker: Asker | None = _DEFAULT,  # type: ignore[assignment]
    ) -> None:
        """
        asker：向用户提问 / 请求确认的方式。默认在交互式终端中使用终端提问，
               非交互环境（服务、后台任务）为 None——此时需确认的操作会被拒绝（除非 auto_confirm）
        """
        self.config = cfg = config or AgentConfig()
        self._home: Path | None = None
        self.asker: Asker | None = default_asker() if asker is _DEFAULT else asker
        self.browser = None
        self._mcp_toolsets: list[Any] = []
        self._mcp_started = False

        # ── 存储：只有用到默认实现时才创建 home 目录 ──
        # 技能层（优先级从低到高）：框架内置 → extra_skill_dirs → 本地可写层
        builtin_layers = [BUILTIN_SKILLS_DIR] if cfg.browser.enabled else []
        repo_layers = [d for d in self._repo_dirs() if d.is_dir()]  # 只加载已同步到本地的仓库（启动时不联网）
        self.store: SkillStore = skill_store or FileSkillStore(
            cfg.skills_dir or self.home / "skills",
            readonly_dirs=[*builtin_layers, *repo_layers, *cfg.extra_skill_dirs],
            stats_dir=self.home / ".stats",
            trusted_dirs=builtin_layers,
            verifier=None if cfg.untrusted_shared == "allow" else self._verify_shared,
        )
        self.runs: RunStore = run_store or SqlRunStore(cfg.database_url or f"sqlite:///{(self.home / 'runs.db').as_posix()}")

        # ── 工具 ──
        self.registry = ToolRegistry()
        if include_builtin_tools:
            for t in builtin_tools(cfg.workspace_dir or self.home / "workspace"):
                self.registry.register(t)
        self.registry.register(make_ask_tool(self.asker))
        if cfg.browser.enabled:
            from no_agent.tools.browser import BrowserSession, make_browser_tools

            self.browser = BrowserSession(cfg.browser)
            for t in make_browser_tools(self.browser):
                self.registry.register(t)
            if cfg.browser.browser_use:
                from no_agent.integrations.browser_use import make_browser_use_tool

                self.registry.register(make_browser_use_tool(self.browser, cfg))
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
            asker=self.asker,
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

    # ── 共享技能：签名与 git 仓库 ──

    def _verify_shared(self, skill_dir: Path) -> tuple[bool, str]:
        from no_agent.skills.signing import verify_skill

        return verify_skill(skill_dir, self.config.trusted_signers)

    def _repo_dirs(self) -> list[Path]:
        return [self._repo_root(r) / r.subdir if r.subdir else self._repo_root(r) for r in self.config.skill_repos]

    def _repo_root(self, repo: Any) -> Path:
        from no_agent.storage.files import slugify

        name = repo.name or slugify(repo.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
        return self.home / "repos" / name

    async def sync_skill_repos(self) -> list[dict[str, Any]]:
        """拉取 / 更新配置的 git 技能仓库，然后重新加载技能（仓库副本只读，本地修改会被覆盖）"""
        import subprocess

        def git(*args: str, cwd: Path | None = None) -> str:
            res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
            if res.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)} 失败: {res.stderr.strip()}")
            return res.stdout.strip()

        def sync_one(repo: Any) -> dict[str, Any]:
            root = self._repo_root(repo)
            if not (root / ".git").exists():
                root.parent.mkdir(parents=True, exist_ok=True)
                git("clone", "--depth", "1", *(["--branch", repo.ref] if repo.ref else []), repo.url, str(root))
            else:
                git("fetch", "--depth", "1", "origin", repo.ref or "HEAD", cwd=root)
                git("reset", "--hard", "FETCH_HEAD", cwd=root)
            return {"url": repo.url, "dir": str(root), "commit": git("rev-parse", "--short", "HEAD", cwd=root)}

        results = []
        for repo in self.config.skill_repos:
            try:
                results.append(await asyncio.to_thread(sync_one, repo))
            except Exception as e:  # noqa: BLE001
                results.append({"url": repo.url, "error": str(e)})
        if isinstance(self.store, FileSkillStore):
            dirs = self._repo_dirs()
            builtin = [d for d in self.store.readonly_dirs if d == BUILTIN_SKILLS_DIR]
            self.store.readonly_dirs = [*builtin, *[d for d in dirs if d.is_dir()], *self.config.extra_skill_dirs]
        self.store.reload()
        return results

    # ── 技能 ──

    def skills(self, include_retired: bool = False) -> list[Skill]:
        return self.store.list(include_retired=include_retired)

    def skill(self, skill_id: str) -> "SkillHandle":
        """
        显式调用某个技能（跳过匹配，行为可预期）：
            run = await agent.skill("fill-form").run({"url": "...", "data": {...}})
        """
        if self.store.get(skill_id) is None:
            raise KeyError(f"技能不存在: {skill_id}")
        return SkillHandle(self, skill_id)

    def install_skill(self, path: str | Path, *, overwrite: bool = False) -> Skill:
        """
        从目录安装技能到本项目（复制）。可以是本框架产出的技能，也可以是普通的 Anthropic Skill（纯 SKILL.md）。
        共享技能也可以不安装，直接用 AgentConfig(extra_skill_dirs=[...]) 以只读方式挂载。
        """
        return self.store.install(Path(path), overwrite=overwrite)

    # ── 生命周期 ──

    def set_asker(self, asker: Asker | None) -> None:
        """更换提问 / 确认方式（例如作为 MCP 服务运行时，改为向 MCP 客户端发起 elicitation）"""
        self.asker = asker
        self.engine.asker = asker
        self.registry.register(make_ask_tool(asker))

    async def start(self) -> None:
        """启动后台进化器，连接配置的 MCP 服务（幂等；run() 会自动调用）"""
        self.evolver.start()
        if self.config.mcp_servers and not self._mcp_started:
            from no_agent.integrations.mcp_client import McpToolset

            self._mcp_started = True
            for cfg in self.config.mcp_servers:
                toolset = McpToolset(cfg)
                try:
                    tools = await toolset.connect()
                except Exception:
                    logger.exception("连接 MCP 服务 %s 失败，跳过", cfg.name)
                    continue
                self._mcp_toolsets.append(toolset)
                for t in tools:
                    self.registry.register(t)
                logger.info("已接入 MCP 服务 %s：%d 个工具", cfg.name, len(tools))

    async def stop(self, *, wait: bool = True) -> None:
        """停止后台进化器；wait=True 时先等待排队中的进化任务完成"""
        if wait:
            await self.evolver.drain()
        await self.evolver.stop()
        if self.browser is not None:
            await self.browser.close()
        for toolset in reversed(self._mcp_toolsets):
            await toolset.close()
        self._mcp_toolsets.clear()
        self._mcp_started = False

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
        await self.start()
        return await self.engine.run(task, mode=mode, skill_id=skill_id, params=params)

    def run_sync(self, task: str, **kwargs: Any) -> Run:
        """同步执行（会等待本次触发的进化任务完成后返回）"""

        async def _main() -> Run:
            try:
                return await self.run(task, **kwargs)
            finally:
                await self.stop()

        return asyncio.run(_main())


class SkillHandle:
    """agent.skill(id) 的返回值"""

    def __init__(self, agent: Agent, skill_id: str) -> None:
        self._agent = agent
        self.id = skill_id

    @property
    def info(self) -> Skill:
        skill = self._agent.store.get(self.id)
        if skill is None:
            raise KeyError(f"技能不存在: {self.id}")
        return skill

    async def run(self, params: dict[str, Any] | None = None, *, task: str | None = None) -> Run:
        """
        params 给定时直接执行（先按 params_schema 校验，不合法抛 ValueError）；
        只给 task 时由小模型从 task 中抽取参数。
        """
        if params is None and task is None:
            params = {}
        text = task or f"执行技能 {self.id}：{json.dumps(params, ensure_ascii=False, default=str)}"
        return await self._agent.run(text, mode="skill", skill_id=self.id, params=params)

    def run_sync(self, params: dict[str, Any] | None = None, *, task: str | None = None) -> Run:
        async def _main() -> Run:
            try:
                return await self.run(params, task=task)
            finally:
                await self._agent.stop()

        return asyncio.run(_main())
