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
from no_agent.skills.models import Permissions, Review, ShadowRecord, Skill, TestCase
from no_agent.skills.compose import SkillCaller
from no_agent.skills.review import review_new, review_patch, trace_tools
from no_agent.skills.runner import Budget, runner_for
from no_agent.skills.runtime import execute_script
from no_agent.skills.script import (
    SCRIPT_API_DOC,
    ScriptError,
    count_llm_calls,
    numbered,
    replace_functions,
    script_skills,
    script_tools,
    validate_script,
)
from no_agent.storage.files import slugify
from no_agent.tools.base import ALWAYS_ALLOWED, ReplayToolbox, ToolRegistry, host_of, truncate

logger = logging.getLogger(__name__)

SKIPPED_MARK = "__skipped__"  # 提炼器判定不值得做成技能的轨迹


@dataclass
class Job:
    kind: Literal["patch", "feedback", "distill", "upgrade", "shadow", "shadow_match"]
    run_id: str
    skill_id: str | None = None
    version: int | None = None


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


DISTILL_SYSTEM = f"""你是技能提炼者。下面是 AI 多次完成的同类任务的执行轨迹。你要把它们泛化为一个可复用、参数化的技能脚本，让以后同类任务直接由脚本完成，不再需要大模型逐步推理。

{SCRIPT_API_DOC}

## 提炼原则
- 找出各轨迹中不变的流程（写成步骤）和变化的部分（变成参数）
- 参数要能从用户的任务描述中抽取出来；用 JSON Schema 描述，写清楚每个参数的含义
- 已有技能（见"可复用的已有技能"）能完成的部分，用 ctx.skill 调用，不要重写
- 可以把通用的子步骤抽成原子技能（atoms），主脚本用 ctx.skill 调用；只抽真正可能在别的任务里复用的部分
- 给出 1~{{n}} 个思路不同的候选脚本（例如一个尽量用确定性代码、一个对页面/数据变化更稳健），系统会回放测试并选出最好的
- guide 要写成独立可读的操作指南（会作为 SKILL.md 正文，其他 Agent 也会直接使用）
- description / when_to_use 要写清楚能力边界，它们会被用来判断新任务是否适用这个技能
- verify 要能识别出"结果看起来成功其实错误"的情况；有不可撤销步骤时写 guard
- 如果这些轨迹不属于同一类任务，只使用与轨迹 1 同类的轨迹（在 use 中列出编号）
- 如果没有可复用的固定流程（一次性问题、纯闲聊、每次做法完全不同），输出 skip

## 输出（只输出 JSON）
{{"skip": false, "use": [1, 2], "id": "kebab-case-id（小写字母数字连字符）", "name": "简短名称", "description": "这个技能做什么（≤300字）",
 "when_to_use": "适用场景与边界", "keywords": ["..."], "params_schema": {{"type": "object", "properties": {{}}, "required": []}},
 "scripts": ["候选脚本1完整源码", "候选脚本2完整源码"], "test_params": [与 use 中每条轨迹一一对应的参数对象],
 "guide": "Markdown 操作指南：给人或其他 Agent 看，不依赖本框架也能照着完成（步骤 + 注意事项 + 常见坑）",
 "atoms": [{{"id": "kebab-case-id", "name": "...", "description": "...", "params_schema": {{...}}, "script": "完整脚本源码"}}]}}
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


JUDGE_SYSTEM = """你在比对同一个任务的两个结果，判断它们是否等价：是否同样正确地完成了任务、给出了相同的关键信息。
忽略字段顺序、格式、措辞、多余的说明文字；关键数据（数值、名称、状态、是否完成）不同就不等价。
只输出 JSON：{"same": true 或 false, "reason": "一句话理由"}"""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


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
        self._queue_loop: asyncio.AbstractEventLoop | None = None  # 队列当前绑定的事件循环
        self.history: deque[dict[str, Any]] = deque(maxlen=100)
        self._task: asyncio.Task | None = None

    # ── 生命周期 ──

    def _bind_queue(self) -> None:
        """
        asyncio.Queue 第一次等待时会绑定到当时的事件循环。run_sync 每次调用都是一个新的事件循环，
        所以换了循环就换一个新队列，把还没处理的任务搬过去。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里（例如同步代码中 submit）：先放进当前队列，start 时再搬
        if self._queue_loop is loop:
            return
        if self._queue_loop is not None:
            old, self.queue = self.queue, asyncio.Queue()
            while not old.empty():
                self.queue.put_nowait(old.get_nowait())
        self._queue_loop = loop

    def start(self) -> None:
        self._bind_queue()
        if self._task is None or self._task.done() or self._task.get_loop() is not self._queue_loop:
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
        self._bind_queue()
        self.queue.put_nowait(job)

    async def drain(self) -> None:
        """等待队列处理完（测试 / 运维用）"""
        self._bind_queue()
        await self.queue.join()

    async def _loop(self) -> None:
        queue = self.queue
        while True:
            job = await queue.get()
            t0 = time.perf_counter()
            outcome: dict[str, Any] = {"status": "cancelled"}
            try:
                with metered() as meter:
                    try:
                        outcome = await self.handle(job)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("进化任务失败: %s", job)
                        outcome = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            finally:
                # 被 stop(wait=False) 取消时也要 task_done，否则之后的 drain() 会永远等下去
                entry = {
                    **asdict(job),
                    **outcome,
                    "tokens": meter.total_tokens,
                    "ms": int((time.perf_counter() - t0) * 1000),
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                self.history.append(entry)
                logger.info("进化任务%s: %s", "被取消" if outcome["status"] == "cancelled" else "完成", entry)
                queue.task_done()

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
        if job.kind == "upgrade":
            return await self._upgrade(run, job.skill_id or "")
        if job.kind == "shadow":
            return await self._shadow(run, job.skill_id or "", job.version)
        if job.kind == "shadow_match":
            return await self._shadow_match(run)
        return {"status": "skipped", "reason": f"未知任务类型 {job.kind}"}

    def _caller(self, overlay: dict[str, tuple[Skill, str]] | None = None, stack: tuple[str, ...] = ()) -> SkillCaller:
        return SkillCaller(self.store, self.llms.fast, step_timeout=self.s.step_timeout, overlay=overlay, stack=stack)

    async def _test(
        self,
        source: str,
        cases: list[TestCase],
        permissions: Permissions | None = None,
        overlay: dict[str, tuple[Skill, str]] | None = None,
    ):
        return await run_tests(
            source, cases, registry=self.registry, llm=self.llms.fast, step_timeout=self.s.step_timeout,
            permissions=permissions, skills=self._caller(overlay),
            runner=runner_for(self.s), budget_factory=lambda: Budget.from_config(self.s),
        )

    @property
    def _use_shadow(self) -> bool:
        return self.s.shadow_min_runs > 0

    @staticmethod
    def _undeclared(skill: Skill, source: str) -> list[str]:
        """脚本用到、但技能没有声明的工具（未声明任何工具的技能不受限）"""
        declared = set(skill.permissions.tools)
        if not declared:
            return []
        return sorted(script_tools(source) - declared - ALWAYS_ALLOWED)

    # ── 修补 ──

    async def _patch(self, run: Run, feedback: str | None = None) -> dict[str, Any]:
        skill = self.store.get(run.skill_id or "")
        if skill is None:
            return {"status": "skipped", "reason": "技能不存在"}
        if (pending := skill.pending_review) is not None:
            return {"status": "skipped", "skill_id": skill.id, "reason": f"v{pending.version} 还在等待审查"}
        trace = run.trace
        params = (trace.get("match") or {}).get("params", {})
        # 有候选版本时在候选的基础上继续改（它是更新的尝试）
        base_version = skill.shadow_version or skill.active_version
        source = self.store.script(skill.id, base_version)

        # 接管成功的运行 → 作为新的回归用例；反馈修补没有"正确答案"，不新增用例
        new_case = None
        if feedback is None:
            new_case = TestCase(task=run.task, params=params, fixtures=trace.get("fixtures", []), source_run_id=run.id)
            # 当前版本 / 候选版本可能已经能处理这种情况了
            if (await self._test(source, [new_case], skill.permissions)).passed:
                self.store.add_test(skill, new_case, self.s.max_test_cases)
                which = "候选版本" if skill.shadow else "当前版本"
                return {"status": "skipped", "skill_id": skill.id, "reason": f"{which}已能通过该用例"}

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
                if undeclared := self._undeclared(skill, new_source):
                    raise ScriptError(
                        f"使用了技能未声明的工具 {', '.join(undeclared)}。技能权限只能由人修改，"
                        f"只能使用已声明的工具：{', '.join(skill.permissions.tools)}"
                    )
            except (ScriptError, KeyError, SyntaxError) as e:
                extra = f"\n\n## 上一次修改不合法\n{e}\n请修正后重新输出。"
                continue

            report = await self._test(new_source, cases, skill.permissions)
            new_case_ok = new_case is None or all(f["case_id"] != new_case.id for f in report.failures)
            if new_case_ok and report.pass_rate >= self.s.promote_min_pass_rate:
                reason = str(out.get("reason", ""))
                if review := review_patch(skill, base_version, source, new_source):  # 扩大了能力 → 等人批准
                    skill = self.store.add_version(skill.id, new_source, reason=reason, pass_rate=report.pass_rate, review=review)
                    if new_case:
                        self.store.add_test(skill, new_case, self.s.max_test_cases)
                    return {
                        "status": "pending_review",
                        "skill_id": skill.id,
                        "version": max(v.version for v in skill.versions),
                        "pass_rate": report.pass_rate,
                        "attempts": attempt,
                        "reason": reason,
                        "review": review.reasons,
                    }
                skill = self.store.add_version(
                    skill.id, new_source, reason=reason, pass_rate=report.pass_rate, activate=not self._use_shadow
                )
                if new_case:
                    self.store.add_test(skill, new_case, self.s.max_test_cases)
                return {
                    "status": "candidate" if self._use_shadow else "promoted",
                    "skill_id": skill.id,
                    "version": skill.shadow_version or skill.active_version,
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
        rejected = [v for v in skill.versions if v.status == "rejected" and v.note]
        if rejected:
            notes = "\n".join(f"- v{v.version}：{v.note}" for v in rejected[-3:])
            parts.append(f"## 之前被淘汰的修补（不要重复同样的问题）\n{notes}")
        if skill.permissions.tools:
            parts.append(f"## 技能权限（只能使用这些工具，不能扩大）\n{', '.join(skill.permissions.tools)}")
        parts.append(f"## 可用工具\n{self.registry.describe()}")
        return "\n\n".join(parts)

    # ── 提炼 ──

    def _run_similarity(self, a: Run, b: Run) -> float:
        # 任务文本 + 用到的工具集合；聚类只是粗筛，最终由提炼器判断是否同类
        return 0.6 * lexical_similarity(a.task, b.task) + 0.4 * _tool_seq_similarity(a, b)

    async def _distill(self, run: Run) -> dict[str, Any]:
        """自由执行轨迹积累够了 → 提炼成新技能"""
        run = self.runs.get(run.id) or run
        if run.distilled_into:
            return {"status": "skipped", "reason": "已提炼"}
        existing = [
            s for s in self.store.list(include_retired=True) if s.status in ("active", "shadow", "pending_review") and s.role == "task"
        ]
        cands = [s for s, _ in await self.matcher.candidates(run.task, existing)]
        if cands and await self.matcher.match(run.task, only=cands):
            return {"status": "skipped", "reason": "已有技能（含影子运行中的）可覆盖该任务"}

        pool = [r for r in self.runs.distill_candidates() if r.id != run.id]
        similar = sorted(((r, self._run_similarity(run, r)) for r in pool), key=lambda x: x[1], reverse=True)
        cluster = [run] + [r for r, s in similar if s >= self.s.distill_similarity][:4]
        if len(cluster) < self.s.distill_min_traces:
            return {"status": "waiting", "traces": len(cluster), "need": self.s.distill_min_traces}

        syn = await self._synthesize(cluster)
        if syn["status"] == "skip":
            self._mark(cluster, SKIPPED_MARK)
            return {"status": "skipped", "reason": syn["reason"]}
        if syn["status"] != "ok":
            return {k: v for k, v in syn.items() if k != "status"} | {"status": syn["status"]}

        skill: Skill = syn["skill"]
        review = review_new(
            "distill", syn["script"], trace_tools=trace_tools(syn["used"]),
            atoms={sid: src for sid, (_, src) in syn["atoms"].items()},
        )
        atoms = self._create_atoms(syn["atoms"], parent=skill.id if review else None)
        skill.tests = syn["cases"]
        if review:
            review.atoms = atoms
        intended = skill.id
        skill = self.store.create(
            skill, syn["script"], reason=f"从 {len(syn['used'])} 条轨迹提炼", pass_rate=syn["pass_rate"], shadow=self._use_shadow,
            review=review,
        )
        if review and skill.id != intended:  # id 冲突被改名：原子技能的审查跟着指向新 id
            for atom_id in atoms:
                atom = self.store.get(atom_id)
                if atom and (v := atom.pending_review) and v.review:
                    v.review.parent = skill.id
                    self.store.save(atom)
        self._mark(syn["used"], skill.id)
        return {
            "status": "pending_review" if review else "shadow" if self._use_shadow else "created",
            "review": review.reasons if review else [],
            "skill_id": skill.id,
            "traces": len(syn["used"]),
            "pass_rate": syn["pass_rate"],
            "attempts": syn["attempts"],
            "candidates": syn["candidates"],
            "atoms": atoms,
        }

    async def _upgrade(self, run: Run, skill_id: str) -> dict[str, Any]:
        """
        按热度升级：指南型技能被成功使用的次数够了 → 从这些轨迹提炼出流程脚本，
        作为候选（影子运行）挂到同一个技能上，转正后这个技能就从"AI 照着指南做"变成"脚本直接执行"
        """
        skill = self.store.get(skill_id)
        if skill is None or skill.kind != "guide":
            return {"status": "skipped", "reason": "不是指南型技能"}
        if skill.shadow:
            return {"status": "skipped", "reason": "已有候选脚本在影子运行"}
        if (pending := skill.pending_review) is not None:
            return {"status": "skipped", "skill_id": skill_id, "reason": f"v{pending.version} 还在等待审查"}
        runs = [
            r for r in self.runs.list(limit=50, skill_id=skill_id)
            if r.mode == "guide" and r.success and r.tool_call_count > 0 and not r.distilled_into
            and (r.feedback_score is None or r.feedback_score >= 0)
        ]
        if len(runs) < self.s.distill_min_traces:
            return {"status": "waiting", "skill_id": skill_id, "traces": len(runs), "need": self.s.distill_min_traces}

        syn = await self._synthesize(runs[:5], upgrade=skill)
        if syn["status"] == "skip":
            self._mark(runs, SKIPPED_MARK)
            return {"status": "skipped", "skill_id": skill_id, "reason": syn["reason"]}
        if syn["status"] != "ok":
            return {k: v for k, v in syn.items() if k != "status"} | {"status": syn["status"], "skill_id": skill_id}

        review = review_new(
            "upgrade", syn["script"], trace_tools=trace_tools(syn["used"]), declared_tools=skill.permissions.tools,
            atoms={sid: src for sid, (_, src) in syn["atoms"].items()},
        )
        atoms = self._create_atoms(syn["atoms"], parent=skill_id if review else None)
        if review:
            review.atoms = atoms
        learned: Skill = syn["skill"]
        skill.params_schema = learned.params_schema  # 指南型原本没有参数；转正前不影响按指南执行
        skill.permissions = learned.permissions
        skill.keywords = sorted(set(skill.keywords) | set(learned.keywords))
        skill.tests = syn["cases"]
        self.store.save(skill)
        skill = self.store.add_version(
            skill_id, syn["script"], reason=f"从 {len(syn['used'])} 次指南执行中提炼", pass_rate=syn["pass_rate"],
            activate=not self._use_shadow, review=review,
        )
        if review:
            self._mark(syn["used"], skill_id)
            return {
                "status": "pending_review", "skill_id": skill_id, "version": max(v.version for v in skill.versions),
                "review": review.reasons, "atoms": atoms,
            }
        self._mark(syn["used"], skill_id)
        return {
            "status": "candidate" if self._use_shadow else "promoted",
            "skill_id": skill_id,
            "version": skill.shadow_version or skill.active_version,
            "traces": len(syn["used"]),
            "candidates": syn["candidates"],
            "atoms": atoms,
        }

    async def _synthesize(self, cluster: list[Run], *, upgrade: Skill | None = None) -> dict[str, Any]:
        """生成多个候选脚本（可附带原子技能）→ 回放测试 → 选最好的；不达标时带着失败信息重试"""
        n = max(1, self.s.distill_candidates)
        system = DISTILL_SYSTEM.replace("{n}", str(n))
        user = self._distill_prompt(cluster, upgrade)
        extra = ""
        for attempt in range(1, self.s.patch_max_attempts + 1):
            out = await complete_json(self.llms.strong, system, user + extra, purpose="distill")
            if out.get("skip"):
                return {"status": "skip", "reason": str(out.get("reason", ""))}
            try:
                scripts = [str(x) for x in (out.get("scripts") or [out["script"]])][:n]
                use = [int(i) for i in (out.get("use") or range(1, len(cluster) + 1))]
                used = [cluster[i - 1] for i in use if 1 <= i <= len(cluster)]
                test_params = out.get("test_params") or []
                if not used or len(test_params) != len(used):
                    raise ValueError("use 与 test_params 数量不一致")
                overlay = self._parse_atoms(out.get("atoms") or [])
                skill = Skill(
                    id=upgrade.id if upgrade else str(out.get("id") or out.get("name") or "skill"),
                    name=upgrade.name if upgrade else str(out.get("name") or out.get("id")),
                    description=upgrade.description if upgrade else str(out["description"]),
                    when_to_use=upgrade.when_to_use if upgrade else str(out.get("when_to_use", "")),
                    keywords=[str(k) for k in out.get("keywords", [])],
                    params_schema=out.get("params_schema") or {"type": "object", "properties": {}},
                    guide=upgrade.guide if upgrade else str(out.get("guide") or ""),
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
            domains = self._seen_domains(used)
            results, problems = [], []
            for k, script in enumerate(scripts, 1):
                try:
                    validate_script(script)
                    called = script_skills(script)
                    unknown = sorted(sid for sid in called if sid not in overlay and self.store.get(sid) is None)
                    if unknown:
                        raise ScriptError(f"调用了不存在的技能 {', '.join(unknown)}")
                except ScriptError as e:
                    problems.append(f"候选 {k} 不合法: {e}")
                    continue
                perms = Permissions(tools=sorted(script_tools(script)), domains=domains, skills=sorted(called))
                report = await self._test(script, cases, perms, overlay=overlay)
                results.append((script, perms, report))
                if report.failures:
                    problems.append(f"候选 {k}（通过率 {report.pass_rate:.0%}）:\n{report.failure_text()}")
            if results:
                # 通过率优先；同等时 ctx.llm 越少越好（更确定、更省），再其次越短越好
                script, perms, report = max(results, key=lambda r: (r[2].pass_rate, -count_llm_calls(r[0]), -len(r[0])))
                if report.pass_rate >= self.s.promote_min_pass_rate:
                    failed = {f["case_id"] for f in report.failures}
                    skill.permissions = perms
                    return {
                        "status": "ok",
                        "skill": skill,
                        "script": script,
                        "cases": [c for c in cases if c.id not in failed][-self.s.max_test_cases :],
                        "pass_rate": report.pass_rate,
                        "used": used,
                        "attempts": attempt,
                        "candidates": len(scripts),
                        "atoms": overlay,
                    }
            extra = "\n\n## 上一次的候选都没有通过测试\n" + "\n".join(problems) + "\n请据此修改。"
        return {"status": "rejected", "reason": "提炼的脚本测试未达标", "detail": extra.strip()}

    def _parse_atoms(self, raw: list[Any]) -> dict[str, tuple[Skill, str]]:
        overlay: dict[str, tuple[Skill, str]] = {}
        for a in raw:
            sid = slugify(str(a.get("id") or a.get("name") or ""))
            if self.store.get(sid) is not None:
                raise ValueError(f"原子技能 id {sid} 已存在，请直接用 ctx.skill 复用它或换一个 id")
            source = str(a["script"])
            validate_script(source)
            atom = Skill(
                id=sid,
                name=str(a.get("name") or sid),
                description=str(a.get("description") or ""),
                params_schema=a.get("params_schema") or {"type": "object", "properties": {}},
                role="atomic",
                permissions=Permissions(tools=sorted(script_tools(source)), skills=sorted(script_skills(source))),
            )
            overlay[sid] = (atom, source)
        return overlay

    def _create_atoms(self, overlay: dict[str, tuple[Skill, str]], parent: str | None = None) -> list[str]:
        """parent 非空：父技能需要审查，原子技能随它一起等待批准"""
        created = []
        for sid, (atom, source) in overlay.items():
            review = Review(kind="distill", reasons=[f"随 {parent} 一起审查"], parent=parent) if parent else None
            created.append(self.store.create(atom, source, reason="提炼时抽出的原子技能", review=review).id)
        return created

    def _seen_domains(self, runs: list[Run]) -> list[str]:
        hosts = set()
        for r in runs:
            for c in r.trace.get("tool_calls", []):
                t = self.registry.get(c["tool"])
                if t and t.url_arg and (h := host_of(str(c["args"].get(t.url_arg, "")))):
                    hosts.add(h)
        return sorted(hosts)

    # ── 影子运行 ──

    async def _shadow(self, run: Run, skill_id: str, version: int | None) -> dict[str, Any]:
        """真实运行之后，用它的录制数据在影子环境里跑一遍候选版本，比对结果"""
        skill = self.store.get(skill_id)
        if skill is None or skill.shadow is None or skill.shadow.version != version:
            return {"status": "skipped", "reason": "候选版本已变化"}
        if not run.success:
            return {"status": "skipped", "reason": "真实运行未成功，无法比对"}
        params = (run.trace.get("match") or {}).get("params", {})
        if run.mode == "guide":  # 指南型执行时没有参数，按候选脚本的参数 schema 抽取
            m = await self.matcher.match(run.task, only=[skill])
            if m is None:
                return {"status": "skipped", "skill_id": skill.id, "reason": "无法为候选脚本抽取参数"}
            params = m.params
        res = await self._run_shadow(skill, skill.shadow.version, run, params)
        if res.guard_rejected:
            return self._record_shadow(skill, run, False, f"候选版本的前置检查拒绝了本该适用的任务: {res.error}")
        return await self._judge_and_record(skill, run, res)

    async def _shadow_match(self, run: Run) -> dict[str, Any]:
        """新提炼的技能还在影子期：看这次运行的任务是否属于它，属于就影子跑一遍"""
        if not run.success:
            return {"status": "skipped", "reason": "真实运行未成功"}
        shadows = [s for s in self.store.list(include_retired=True) if s.status == "shadow" and s.shadow]
        if not getattr(runner_for(self.s), "sandboxed", False):  # 不可信的共享技能只能在 sandbox 档下执行
            shadows = [s for s in shadows if s.id not in getattr(self.store, "untrusted", ())]
        cands = [s for s, _ in await self.matcher.candidates(run.task, shadows)]
        m = await self.matcher.match(run.task, only=cands) if cands else None
        if m is None or m.skill.shadow is None:
            return {"status": "skipped", "reason": "没有匹配的影子技能"}
        res = await self._run_shadow(m.skill, m.skill.shadow.version, run, m.params)
        if res.guard_rejected:  # 新技能自己判定不适用：说明匹配错了，不计入
            return {"status": "skipped", "skill_id": m.skill.id, "reason": f"影子技能判定不适用: {res.error}"}
        return await self._judge_and_record(m.skill, run, res)

    async def _run_shadow(self, skill: Skill, version: int, run: Run, params: dict[str, Any]):
        runner = runner_for(self.s)
        if skill.id in getattr(self.store, "untrusted", ()) and not getattr(runner, "sandboxed", False):
            raise RuntimeError(f"拒绝在 sandbox 档之外执行不可信的共享技能 {skill.id}")
        toolbox = ReplayToolbox(self.registry, run.trace.get("fixtures", []), timeout=self.s.tool_timeout)
        toolbox.restrict(skill.permissions.tools, skill.permissions.domains)
        return await execute_script(
            self.store.script(skill.id, version),
            task=run.task,
            params=params,
            toolbox=toolbox,
            llm=self.llms.fast,
            step_timeout=self.s.step_timeout,
            skills=self._caller(stack=(skill.id,)),
            skill_allowlist=skill.permissions.skills,
            runner=runner,
            budget=Budget.from_config(self.s),
        )

    async def _judge_and_record(self, skill: Skill, run: Run, res) -> dict[str, Any]:
        if not res.success:
            return self._record_shadow(skill, run, False, f"候选版本在 {res.failed_step} 失败: {res.error}")
        agree, reason = await self._agree(run.task, run.result, res.result)
        return self._record_shadow(skill, run, agree, reason)

    async def _agree(self, task: str, real: Any, shadow: Any) -> tuple[bool, str]:
        if _canonical(real) == _canonical(shadow):
            return True, "结果完全一致"
        user = (
            f"## 任务\n{task}\n\n## 结果 A（实际交付给用户的）\n{json.dumps(truncate(real, 3000), ensure_ascii=False, default=str)}"
            f"\n\n## 结果 B（候选脚本产出的）\n{json.dumps(truncate(shadow, 3000), ensure_ascii=False, default=str)}"
        )
        try:
            out = await complete_json(self.llms.fast, JUDGE_SYSTEM, user, purpose="shadow_judge")
        except ValueError as e:
            return False, f"比对失败: {e}"
        return bool(out.get("same")), str(out.get("reason", ""))

    def _record_shadow(self, skill: Skill, run: Run, agree: bool, reason: str) -> dict[str, Any]:
        st = skill.shadow
        assert st is not None
        st.log = (st.log + [ShadowRecord(run_id=run.id, agree=agree, reason=reason[:300])])[-20:]
        if agree:
            st.passes += 1
        else:
            st.failures += 1
        base = {"skill_id": skill.id, "version": st.version, "passes": st.passes, "failures": st.failures, "reason": reason}
        if st.failures > self.s.shadow_max_failures:
            self.store.reject_shadow(skill.id, f"影子运行未通过：{reason}")
            return {**base, "status": "shadow_rejected"}
        if st.passes >= self.s.shadow_min_runs:
            self.store.promote(skill.id)
            return {**base, "status": "promoted"}
        self.store.save_shadow(skill)
        return {**base, "status": "shadow_pass" if agree else "shadow_fail"}

    def _distill_prompt(self, cluster: list[Run], upgrade: Skill | None = None) -> str:
        parts = []
        if upgrade is not None:
            parts.append(
                f"## 这是指南型技能「{upgrade.id}」的使用轨迹\n它目前由 AI 照着下面的指南执行。请把它固化为流程脚本"
                f"（id、name、description 沿用原技能，guide 可以留空）：\n\n{upgrade.guide[:4000]}\n"
            )
        for i, r in enumerate(cluster, 1):
            parts.append(
                f"### 轨迹 {i}\n任务: {r.task}\n工具调用:\n{_fmt_calls(r.trace.get('tool_calls', []), 800)}\n"
                f"最终结果: {json.dumps(truncate(r.result, 1500), ensure_ascii=False, default=str)}"
            )
        reusable = [
            s for s in self.store.list() if s.kind == "flow" and (upgrade is None or s.id != upgrade.id)
        ][:30]
        lib = "\n".join(
            f"- {s.id}（{'原子' if s.role == 'atomic' else '任务'}）参数 {json.dumps(s.params_schema.get('properties', {}), ensure_ascii=False)}：{s.description}"
            for s in reusable
        )
        head, body = (parts[0] + "\n", parts[1:]) if upgrade is not None else ("", parts)
        return (
            head + "## 执行轨迹\n\n" + "\n\n".join(body)
            + f"\n\n## 可用工具\n{self.registry.describe()}"
            + (f"\n\n## 可复用的已有技能（用 ctx.skill 调用）\n{lib}" if lib else "")
        )

    def _mark(self, runs: list[Run], value: str) -> None:
        for r in runs:
            r.distilled_into = value
            self.runs.save(r)
