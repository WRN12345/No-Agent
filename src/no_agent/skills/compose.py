"""
技能组合：脚本里用 await ctx.skill("login", user=...) 调用另一个流程型技能

- 子技能在自己声明的权限下运行（能力委托：调用方必须声明可调用它）；
  子技能未声明的工具 / 域名不会因此放开，而是继承调用方的限制
- 不可信的共享技能不能被调用
- 子技能失败 → 调用它的步骤失败（父脚本整体由 AI 接管、修补）
- 用户取消会一路向上传递
- 嵌套深度有上限，防止循环调用
- overlay：提炼时新生成、尚未入库的原子技能，测试阶段通过 overlay 提供
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from no_agent.skills.models import Skill, validate_params
from no_agent.tools.base import UserAbort

if TYPE_CHECKING:
    from no_agent.llm import BaseLLM
    from no_agent.storage.base import SkillStore
    from no_agent.tools.base import Toolbox

MAX_DEPTH = 3


class SkillCallError(Exception):
    """子技能调用失败（由 ctx.skill 转成步骤失败）"""


class SkillCaller:
    def __init__(
        self,
        store: SkillStore,
        llm: BaseLLM,
        *,
        step_timeout: float = 60.0,
        overlay: dict[str, tuple[Skill, str]] | None = None,
        depth: int = 0,
        stack: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.llm = llm
        self.step_timeout = step_timeout
        self.overlay = overlay or {}
        self.depth = depth
        self.stack = stack

    def _resolve(self, skill_id: str) -> tuple[Skill, str]:
        if skill_id in self.overlay:
            return self.overlay[skill_id]
        skill = self.store.get(skill_id)
        if skill is None or skill.status == "retired":
            raise SkillCallError(f"技能不存在或已下线: {skill_id}")
        if skill.status == "pending_review":
            raise SkillCallError(f"{skill_id} 还在等待审查，不能被调用")
        if skill_id in getattr(self.store, "untrusted", ()):
            raise SkillCallError(f"{skill_id} 是不可信的共享技能（{self.store.trust.get(skill_id, '未签名')}），不能被调用")
        if skill.kind != "flow":
            raise SkillCallError(f"{skill_id} 是指南型技能，不能被脚本调用")
        return skill, self.store.script(skill_id)

    async def call(
        self, skill_id: str, params: dict[str, Any], *, toolbox: Toolbox, task: str, runner: Any = None, budget: Any = None
    ) -> Any:
        from no_agent.skills.runtime import execute_script

        if skill_id in self.stack:
            raise SkillCallError(f"循环调用: {' → '.join(self.stack + (skill_id,))}")
        if self.depth >= MAX_DEPTH:
            raise SkillCallError(f"技能嵌套超过 {MAX_DEPTH} 层")
        skill, source = self._resolve(skill_id)
        if errors := validate_params(skill.params_schema, params):
            raise SkillCallError(f"调用 {skill_id} 的参数不合法：" + "；".join(errors))

        child = SkillCaller(
            self.store, self.llm, step_timeout=self.step_timeout, overlay=self.overlay,
            depth=self.depth + 1, stack=self.stack + (skill_id,),
        )
        # 子技能在自己声明的权限下运行；未声明的部分继承调用方的限制。不修改共享的 toolbox，并发调用互不干扰
        with toolbox.delegated(skill.permissions.tools, skill.permissions.domains):
            res = await execute_script(
                source, task=task, params=params, toolbox=toolbox, llm=self.llm, step_timeout=self.step_timeout,
                skills=child, skill_allowlist=skill.permissions.skills, runner=runner, budget=budget,
            )
        if res.aborted:
            raise UserAbort(res.error or "用户取消")
        if not res.success:
            raise SkillCallError(f"子技能 {skill_id} 在 {res.failed_step} 失败: {res.error}")
        return res.result
