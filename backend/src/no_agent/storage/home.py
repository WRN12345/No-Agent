"""
数据根目录（home）的解析与初始化

解析顺序（与 git 查找 .git/ 类似，不会因为在子目录启动而悄悄另起一份数据）：
  1. 显式传入 AgentConfig(home=...)
  2. 环境变量 NO_AGENT_HOME
  3. 从当前目录向上查找已存在的 .no-agent/
  4. 当前目录下新建 .no-agent/

目录结构：
  .no-agent/
    skills/       学到的技能（建议提交到 git，可审查、可回滚、可在团队间共享）
    runs.db       运行轨迹（不提交）
    workspace/    工具工作区（不提交）
    .stats/       技能运行统计（不提交）
    .gitignore    自动生成
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

HOME_DIRNAME = ".no-agent"
HOME_ENV = "NO_AGENT_HOME"

_GITIGNORE = """\
# 由 No-Agent 自动生成：技能（skills/）建议提交，运行数据不提交
runs.db
runs.db-*
workspace/
.stats/
"""


def resolve_home(explicit: str | Path | None = None, start: Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if env := os.environ.get(HOME_ENV):
        return Path(env).expanduser().resolve()
    cwd = (start or Path.cwd()).resolve()
    for d in (cwd, *cwd.parents):
        if (d / HOME_DIRNAME).is_dir():
            return d / HOME_DIRNAME
    return cwd / HOME_DIRNAME


def init_home(home: Path) -> Path:
    """创建目录结构（幂等）"""
    created = not home.exists()
    home.mkdir(parents=True, exist_ok=True)
    gi = home / ".gitignore"
    if not gi.exists():
        gi.write_text(_GITIGNORE, encoding="utf-8")
    if created:
        logger.info("初始化 No-Agent 数据目录: %s", home)
    return home
