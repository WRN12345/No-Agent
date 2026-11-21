"""
browser-use 集成：browser_agent 工具

适用场景：页面结构未知、需要多步探索的网页子任务（登录、搜索、翻页、在导航菜单里找到某个页面）。
browser-use 通过 CDP 接入本框架启动的同一个浏览器，完成后本框架的确定性工具
（browser_snapshot / browser_fill / browser_submit …）可以在它停下的页面上继续操作。

分工：
  browser-use      → 开放式探索（每次都要大模型逐步推理，贵、不确定）
  browser_* 工具    → 确定性操作（可录制、回放、提炼成脚本）
所以 browser_agent 适合做"把页面带到正确位置"，可重复的部分交给确定性工具，才能沉淀成省 token 的技能。

安全：browser-use 的点击不经过本框架的提交确认，因此调用 browser_agent 本身默认需要用户确认
（BrowserConfig.browser_use_confirm），并要求它不要执行提交、支付等不可撤销操作。

需要 pip install "No-Agent[browser-use]"（已验证 browser-use 1.0.x 的接口）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from no_agent.tools.base import Tool, ToolError

if TYPE_CHECKING:
    from no_agent.config import AgentConfig
    from no_agent.tools.browser import BrowserSession

logger = logging.getLogger(__name__)

SAFETY_NOTE = (
    "\n\n重要约束：不要点击提交、确认、付款、删除、发送等不可撤销的按钮；"
    "需要提交时停在提交前的页面，并在最终结果中说明。不要编造任何信息。"
)


def make_browser_use_tool(session: BrowserSession, cfg: AgentConfig) -> Tool:
    async def browser_agent(task: str, max_steps: int | None = None) -> dict[str, Any]:
        try:
            from browser_use import Agent as BUAgent
            from browser_use import Browser, ChatOpenAI
        except ImportError as e:
            raise ToolError('browser_agent 需要额外依赖：pip install "No-Agent[browser-use]"') from e

        await session.page()  # 确保浏览器已启动
        if not session.cdp_url:
            raise ToolError("浏览器没有开启 CDP 端口（需要 BrowserConfig.browser_use=True）")

        llm_cfg = cfg.llm
        llm = ChatOpenAI(
            model=cfg.browser.browser_use_model or llm_cfg.strong_model,
            base_url=llm_cfg.strong_base_url or llm_cfg.base_url,
            api_key=llm_cfg.strong_api_key or llm_cfg.api_key,
        )
        browser = Browser(cdp_url=session.cdp_url, keep_alive=True)
        agent = BUAgent(task=task + SAFETY_NOTE, llm=llm, browser=browser, judge=False)
        try:
            history = await agent.run(max_steps=max_steps or cfg.browser.browser_use_max_steps)
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"browser-use 执行失败: {e}") from e
        page = await session.adopt_latest_page()
        errors = [str(x) for x in (history.errors() or []) if x][:3]
        return {
            "done": bool(history.is_done()),
            "success": history.is_successful(),
            "result": history.final_result(),
            "steps": history.number_of_steps(),
            "errors": errors,
            "url": page.url,
            "title": await page.title(),
        }

    return Tool(
        name="browser_agent",
        description=(
            "把一个开放式的网页子任务交给 browser-use 浏览器 Agent 自主完成（适合页面结构未知、需要多步探索："
            "登录、搜索、翻页、在菜单里找到目标页面）。它在同一个浏览器里操作，完成后可以继续用 browser_* 工具"
            "（如 browser_snapshot / browser_fill）在它停下的页面上做确定性操作。不会帮你提交表单。"
            "比 browser_* 工具贵得多，能用确定性工具完成的不要用它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "要完成的网页子任务，写清楚目标和停止条件"},
                "max_steps": {"type": "integer", "description": f"最大步数，默认 {cfg.browser.browser_use_max_steps}"},
            },
            "required": ["task"],
        },
        func=browser_agent,
        side_effect=True,
        stateful=True,
        confirm=cfg.browser.browser_use_confirm,
        confirm_message=lambda a: f"即将让 browser-use 自主操作浏览器：\n  {a.get('task', '')}",
    )
