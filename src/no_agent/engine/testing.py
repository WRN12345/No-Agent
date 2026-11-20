"""
上线门禁：用录制的测试用例回放脚本

- 录制过的工具调用直接回放（确定、零成本）
- 有副作用的调用不真正执行
- 通过 = 脚本跑完且 verify 通过
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from no_agent.llm import BaseLLM
from no_agent.skills.models import Permissions, TestCase
from no_agent.skills.runtime import execute_script
from no_agent.tools.base import ReplayToolbox, ToolRegistry


@dataclass
class TestReport:
    __test__ = False

    total: int = 0
    passed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def failure_text(self, limit: int = 5) -> str:
        return "\n".join(
            f"- 用例 {f['case_id']}（参数 {f['params']}）在步骤 {f['step']} 失败: {f['error']}" for f in self.failures[:limit]
        )


async def run_tests(
    source: str,
    cases: list[TestCase],
    *,
    registry: ToolRegistry,
    llm: BaseLLM,
    step_timeout: float = 60.0,
    concurrency: int = 4,
    permissions: Permissions | None = None,
    skills: Any = None,
) -> TestReport:
    sem = asyncio.Semaphore(concurrency)

    async def one(case: TestCase):
        async with sem:
            toolbox = ReplayToolbox(registry, case.fixtures)
            if permissions is not None:
                toolbox.restrict(permissions.tools, permissions.domains)
            return case, await execute_script(
                source, task=case.task, params=case.params, toolbox=toolbox, llm=llm, step_timeout=step_timeout,
                skills=skills, skill_allowlist=permissions.skills if permissions else None,
            )

    report = TestReport(total=len(cases))
    for case, res in await asyncio.gather(*(one(c) for c in cases)):
        if res.success:
            report.passed += 1
        else:
            report.failures.append({"case_id": case.id, "params": case.params, "step": res.failed_step, "error": res.error})
    return report
