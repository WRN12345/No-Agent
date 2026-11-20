"""
示例：让 Agent 在浏览器里填写表单

    pip install -e ".[browser]"
    cp .env.example .env              # 填好模型配置
    python examples/fill_form.py <表单网址> "要填写的信息"

流程：框架启动 Chrome → 读取页面全部字段 → 用你给的信息自动填 → 缺的必填项在终端里一次问完
     → 回读校验 → 在终端里确认后提交
"""

import asyncio
import sys

from no_agent import Agent, AgentConfig, BrowserConfig


async def main(url: str, info: str) -> None:
    config = AgentConfig.from_env(browser=BrowserConfig(enabled=True))
    async with Agent(config) as agent:
        run = await agent.run(f"在 {url} 填写表单。已知信息：{info}")
        print()
        print(f"方式: {run.mode}    成功: {run.success}    token: {run.prompt_tokens + run.completion_tokens}")
        print(f"结果: {run.result if run.success else run.error}")
        input("\n按回车关闭浏览器...")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:]) or "（无）"))
