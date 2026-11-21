"""
No-Agent：自进化 Agent 框架（包名 No-Agent，导入名 no_agent，环境变量前缀 NO_AGENT_）

能用脚本就用脚本；脚本坏了 AI 接管并自动修补；没见过的任务 AI 自己做，做多了沉淀成技能。
"""

from no_agent.agent import Agent
from no_agent.config import AgentConfig, BrowserConfig, LLMConfig
from no_agent.interact import Asker, Question, ScriptedAsker, TerminalAsker
from no_agent.llm import BaseLLM, LLMs
from no_agent.skills.models import Skill
from no_agent.storage import FileSkillStore, Run, RunStore, SkillStore, SqlRunStore
from no_agent.tools import Tool, ToolError, UserAbort, tool

__version__ = "0.2.0"

__all__ = [
    "Agent",
    "AgentConfig",
    "Asker",
    "BrowserConfig",
    "Question",
    "ScriptedAsker",
    "TerminalAsker",
    "UserAbort",
    "BaseLLM",
    "FileSkillStore",
    "LLMConfig",
    "LLMs",
    "Run",
    "RunStore",
    "Skill",
    "SkillStore",
    "SqlRunStore",
    "Tool",
    "ToolError",
    "tool",
]
