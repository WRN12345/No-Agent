"""
进化器：后台异步运行，不阻塞用户请求

- patch：脚本失败 → AI 接管成功后，参考接管过程对脚本做最小修补，测试通过自动上线
- feedback：脚本"成功"但用户反馈结果不对（静默错误）→ 修补脚本并加强 verify
- distill：积累到足够多相似的自由执行轨迹后，泛化提炼为新技能

单 worker 串行处理，避免并发提炼出重复技能。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from no_agent.config import AgentConfig
from no_agent.engine.testing import run_tests
from no_agent.llm import LLMs, complete_json, metered
from no_agent.storage.base import RunStore, SkillStore
from no_agent.storage.sql import Run
from no_agent.skills.matcher import SkillMatcher, lexical_similarity
from no_agent.skills.models import Skill, TestCase
from no_agent.skills.script import SCRIPT_API_DOC, ScriptError, numbered, replace_functions, validate_script
from no_agent.tools.base import ToolRegistry, truncate

logger = logging.getLogger(__name__)

SKIPPED_MARK = "__skipped__"  # 提炼器判定不值得做成技能的轨迹


@dataclass
class Job:
    kind: Literal["patch", "feedback", "distill"]
    run_id: str


PATCH_SYSTEM = f"""你是技能脚本的维护者。一个自动化技能脚本执行出了问题，你要修改脚本，让它以后能自动正确处理这种情况。

{SCRIPT_API_DOC}

## 修补原则
- 最小修改：优先只重写出问题的步骤函数；只有流程结构必须改变时才重写整个脚本
- 参考"AI 接管过程"：它展示了在当前环境下怎样做才能成功，把它的做法固化到脚本里
- 保持参数接口兼容（新增参数必须有默认值）
- 让脚本更健壮，而不是只适配这一次的数据
- 如果问题是结果错误但没被发现，必须同时加强 verify
- 如果失败是偶发的（网络抖动、临时限流），不需要改代码，输出 give_up

## 输出（只输出 JSON）
只改部分函数：{{"mode": "functions", "functions": {{"函数名": "完整函数源码（含装饰器）"}}, "reason": "改了什么、为什么"}}
重写整个脚本：{{"mode": "script", "script": "完整脚本", "reason": "..."}}
无需修改：{{"give_up": true, "reason": "..."}}"""


DISTILL_SYSTEM = f"""你是技能提炼者。下面是 AI 多次自由完成的同类任务的执行轨迹。你要把它们泛化为一个可复用、参数化的技能脚本，让以后同类任务直接由脚本完成，不再需要大模型逐步推理。

{SCRIPT_API_DOC}

## 提炼原则
- 找出各轨迹中不变的流程（写成步骤）和变化的部分（变成参数）
- 参数要能从用户的任务描述中抽取出来；用 JSON Schema 描述，写清楚每个参数的含义
- description / when_to_use 要写清楚能力边界，它们会被用来判断新任务是否适用这个技能
- verify 要能识别出"结果看起来成功其实错误"的情况
- 如果这些轨迹不属于同一类任务，只使用与轨迹 1 同类的轨迹（在 use 中列出编号）
- 如果没有可复用的固定流程（一次性问题、纯闲聊、每次做法完全不同），输出 skip

## 输出（只输出 JSON）
{{"skip": false, "use": [1, 2], "id": "snake_case_id", "name": "简短名称", "description": "这个技能做什么",
 "when_to_use": "适用场景与边界", "keywords": ["..."], "params_schema": {{"type": "object", "properties": {{}}, "required": []}},
 "script": "完整脚本源码", "test_params": [与 use 中每条轨迹一一对应的参数对象]}}
或：{{"skip": true, "reason": "..."}}"""


def _fmt_calls(calls: list[dict[str, Any]], limit: int = 1200) -> str:
    lines = []
    for i, c in enumerate(calls, 1):
        out = c.get("output") if c.get("ok") else f"错误: {c.get('error')}"
        lines.append(
            f"{i}. [{c.get('step') or '-'}] {c['tool']}({json.dumps(c['args'], ensure_ascii=False)}) → "
            f"{json.dumps(truncate(out, limit), ensure_ascii=False, default=str)}"
        )
    return "\n".join(lines) or "（无）"


def _tool_seq_similarity(a: Run, b: Run) -> float:
    ta = {c["tool"] for c in a.trace.get("tool_calls", [])}
    tb = {c["tool"] for c in b.trace.get("tool_calls", [])}
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


class Evolver:
    def __init__(
        self,
        *,
        store: SkillStore,
        runs: RunStore,
        registry: ToolRegistry,
        llms: LLMs,
        matcher: SkillMatcher,
        settings: AgentConfig,
    ) -> None:
        self.store = store
        self.runs = runs
        self.registry = registry
        self.llms = llms
        self.matcher = matcher
        self.s = settings
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.history: deque[dict[str, Any]] = deque(maxlen=100)
        self._task: asyncio.Task | None = None

    # ── 生命周期 ──

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="evolver")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def submit(self, job: Job) -> None:
        self.queue.put_nowait(job)

    async def drain(self) -> None:
        """等待队列处理完（测试 / 运维用）"""
        await self.queue.join()

    async def _loop(self) -> None:
        while True:
            job = await self.queue.get()
            t0 = time.perf_counter()
            with metered() as meter:
                try:
                    outcome = await self.handle(job)
                except Exception as e:  # noqa: BLE001
                    logger.exception("进化任务失败: %s", job)
                    outcome = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            entry = {
                **asdict(job),
                **outcome,
                "tokens": meter.total_tokens,
                "ms": int((time.perf_counter() - t0) * 1000),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            self.history.append(entry)
            logger.info("进化任务完成: %s", entry)
            self.queue.task_done()

    async def handle(self, job: Job) -> dict[str, Any]:
        run = self.runs.get(job.run_id)
        if run is None:
            return {"status": "skipped", "reason": "运行记录不存在"}
        if job.kind == "patch":
            return await self._patch(run)
        if job.kind == "feedback":
            return await self._patch(run, feedback=run.feedback_comment or "用户认为结果不正确")
        if job.kind == "distill":
            return await self._distill(run)
        return {"status": "skipped", "reason": f"未知任务类型 {job.kind}"}

    async def _test(self, source: str, cases: list[TestCase]):
        return await run_tests(source, cases, registry=self.registry, llm=self.llms.fast, step_timeout=self.s.step_timeout)

    # ── 修补 ──

    async def _patch(self, run: Run, feedback: str | None = None) -> dict[str, Any]:
        skill = self.store.get(run.skill_id or "")
        if skill is None:
            return {"status": "skipped", "reason": "技能不存在"}
        trace = run.trace
        params = (trace.get("match") or {}).get("params", {})
        source = self.store.script(skill.id)

        # 接管成功的运行 → 作为新的回归用例；反馈修补没有"正确答案"，不新增用例
        new_case = None
        if feedback is None:
            new_case = TestCase(task=run.task, params=params, fixtures=trace.get("fixtures", []), source_run_id=run.id)
            # 当前版本可能已经被别的任务修好了
            if (await self._test(source, [new_case])).passed:
                self.store.add_test(skill, new_case, self.s.max_test_cases)
                return {"status": "skipped", "skill_id": skill.id, "reason": "当前版本已能通过该用例"}

        cases = skill.tests + ([new_case] if new_case else [])
        if not cases:
            return {"status": "rejected", "skill_id": skill.id, "reason": "没有测试用例，无法验证修补"}

        user = self._patch_prompt(run, skill, source, params, feedback)
        extra = ""
        for attempt in range(1, self.s.patch_max_attempts + 1):
            out = await complete_json(self.llms.strong, PATCH_SYSTEM, user + extra, purpose="patch")
            if out.get("give_up"):
                return {"status": "skipped", "skill_id": skill.id, "reason": out.get("reason", "")}
            try:
                if out.get("mode") == "script":
                    new_source = str(out["script"])
                else:
                    new_source = replace_functions(source, {k: str(v) for k, v in (out.get("functions") or {}).items()})
                validate_script(new_source)
            except (ScriptError, KeyError, SyntaxError) as e:
                extra = f"\n\n## 上一次修改不合法\n{e}\n请修正后重新输出。"
                continue

            report = await self._test(new_source, cases)
            new_case_ok = new_case is None or all(f["case_id"] != new_case.id for f in report.failures)
            if new_case_ok and report.pass_rate >= self.s.promote_min_pass_rate:
                reason = str(out.get("reason", ""))
                skill = self.store.add_version(skill.id, new_source, reason=reason, pass_rate=report.pass_rate)
                if new_case:
                    self.store.add_test(skill, new_case, self.s.max_test_cases)
                return {
                    "status": "promoted",
                    "skill_id": skill.id,
                    "version": skill.active_version,
                    "pass_rate": report.pass_rate,
                    "attempts": attempt,
                    "reason": reason,
                }
            extra = f"\n\n## 上一次修改后的测试结果（通过率 {report.pass_rate:.0%}）\n{report.failure_text()}\n请据此再修改。"

        return {"status": "rejected", "skill_id": skill.id, "reason": "修补后测试未达标", "detail": extra.strip()}

    def _patch_prompt(self, run: Run, skill: Skill, source: str, params: dict, feedback: str | None) -> str:
        trace = run.trace
        exe = trace.get("execution") or {}
        parts = [
            f"## 技能\n{skill.id} v{skill.active_version}：{skill.description}",
            f"## 任务\n{run.task}",
            f"## 参数\n{json.dumps(params, ensure_ascii=False)}",
            f"## 当前脚本\n```python\n{numbered(source)}\n```",
        ]
        if feedback is None:
            takeover_calls = [c for c in trace.get("tool_calls", []) if c.get("step") == "takeover"]
            script_calls = [c for c in trace.get("tool_calls", []) if c.get("step") != "takeover"]
            parts += [
                f"## 脚本执行情况\n失败步骤: {exe.get('failed_step')}\n错误: {exe.get('error')}",
                f"## 脚本的工具调用\n{_fmt_calls(script_calls)}",
                f"## 失败时 ctx.state\n{json.dumps(trace.get('state_at_failure', {}), ensure_ascii=False)[:4000]}",
                f"## AI 接管过程（成功完成了任务）\n{_fmt_calls(takeover_calls)}",
                f"## 接管后的最终结果\n{json.dumps(truncate(run.result, 2000), ensure_ascii=False, default=str)}",
            ]
        else:
            parts += [
                f"## 问题\n脚本运行'成功'且 verify 通过，但用户反馈结果不对：\n{feedback}",
                f"## 脚本的工具调用\n{_fmt_calls(trace.get('tool_calls', []))}",
                f"## 脚本输出的结果\n{json.dumps(truncate(run.result, 2000), ensure_ascii=False, default=str)}",
            ]
        parts.append(f"## 可用工具\n{self.registry.describe()}")
        return "\n\n".join(parts)

    # ── 提炼 ──

    def _run_similarity(self, a: Run, b: Run) -> float:
        # 任务文本 + 用到的工具集合；聚类只是粗筛，最终由提炼器判断是否同类
        return 0.6 * lexical_similarity(a.task, b.task) + 0.4 * _tool_seq_similarity(a, b)

    async def _distill(self, run: Run) -> dict[str, Any]:
        run = self.runs.get(run.id) or run
        if run.distilled_into:
            return {"status": "skipped", "reason": "已提炼"}
        if self.store.list() and await self.matcher.match(run.task):
            return {"status": "skipped", "reason": "已有技能可覆盖该任务"}

        pool = [r for r in self.runs.distill_candidates() if r.id != run.id]
        similar = sorted(((r, self._run_similarity(run, r)) for r in pool), key=lambda x: x[1], reverse=True)
        cluster = [run] + [r for r, s in similar if s >= self.s.distill_similarity][:4]
        if len(cluster) < self.s.distill_min_traces:
            return {"status": "waiting", "traces": len(cluster), "need": self.s.distill_min_traces}

        user = self._distill_prompt(cluster)
        extra = ""
        for attempt in range(1, self.s.patch_max_attempts + 1):
            out = await complete_json(self.llms.strong, DISTILL_SYSTEM, user + extra, purpose="distill")
            if out.get("skip"):
                self._mark(cluster, SKIPPED_MARK)
                return {"status": "skipped", "reason": out.get("reason", "")}
            try:
                script = str(out["script"])
                validate_script(script)
                use = [int(i) for i in (out.get("use") or range(1, len(cluster) + 1))]
                used = [cluster[i - 1] for i in use if 1 <= i <= len(cluster)]
                test_params = out.get("test_params") or []
                if not used or len(test_params) != len(used):
                    raise ValueError("use 与 test_params 数量不一致")
                skill = Skill(
                    id=str(out.get("id") or out.get("name") or "skill"),
                    name=str(out.get("name") or out.get("id")),
                    description=str(out["description"]),
                    when_to_use=str(out.get("when_to_use", "")),
                    keywords=[str(k) for k in out.get("keywords", [])],
                    params_schema=out.get("params_schema") or {"type": "object", "properties": {}},
                )
            except (ScriptError, KeyError, ValueError, TypeError) as e:
                extra = f"\n\n## 上一次输出不合法\n{e}\n请修正后重新输出。"
                continue

            if len(used) < self.s.distill_min_traces:
                return {"status": "waiting", "reason": "同类轨迹不足", "traces": len(used)}

            cases = [
                TestCase(task=r.task, params=p if isinstance(p, dict) else {}, fixtures=r.trace.get("fixtures", []), source_run_id=r.id)
                for r, p in zip(used, test_params)
            ]
            report = await self._test(script, cases)
            if report.pass_rate >= self.s.promote_min_pass_rate:
                failed = {f["case_id"] for f in report.failures}
                skill.tests = [c for c in cases if c.id not in failed][-self.s.max_test_cases :]
                skill = self.store.create(skill, script, reason=f"从 {len(used)} 条轨迹提炼", pass_rate=report.pass_rate)
                self._mark(used, skill.id)
                return {"status": "created", "skill_id": skill.id, "traces": len(used), "pass_rate": report.pass_rate, "attempts": attempt}
            extra = f"\n\n## 上一次脚本的测试结果（通过率 {report.pass_rate:.0%}）\n{report.failure_text()}\n请据此修改。"

        return {"status": "rejected", "reason": "提炼的脚本测试未达标", "detail": extra.strip()}

    def _distill_prompt(self, cluster: list[Run]) -> str:
        parts = []
        for i, r in enumerate(cluster, 1):
            parts.append(
                f"### 轨迹 {i}\n任务: {r.task}\n工具调用:\n{_fmt_calls(r.trace.get('tool_calls', []), 800)}\n"
                f"最终结果: {json.dumps(truncate(r.result, 1500), ensure_ascii=False, default=str)}"
            )
        return "## 执行轨迹\n\n" + "\n\n".join(parts) + f"\n\n## 可用工具\n{self.registry.describe()}"

    def _mark(self, runs: list[Run], value: str) -> None:
        for r in runs:
            r.distilled_into = value
            self.runs.save(r)
