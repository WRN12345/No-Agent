"""
命令行入口（配置从 NO_AGENT_* 环境变量 / .env 读取）

    no-agent mcp                启动 MCP 服务（stdio），供 Claude Desktop / Claude Code / Cursor 等接入
    no-agent serve [--port N]   启动 HTTP 服务
    no-agent skills             列出技能
    no-agent install <目录>     安装技能（SKILL.md 目录）

在 MCP 客户端中配置，例如：
    {"mcpServers": {"no-agent": {"command": "no-agent", "args": ["mcp"], "env": {"NO_AGENT_HOME": "/path/to/project/.no-agent"}}}}
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def _agent(**overrides):
    from no_agent import Agent, AgentConfig

    return Agent(AgentConfig.from_env(**overrides))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="no-agent", description="自进化 Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="以 stdio 方式启动 MCP 服务")
    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("skills", help="列出技能")
    p_install = sub.add_parser("install", help="安装技能目录")
    p_install.add_argument("path")
    p_install.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    # stdio MCP 用 stdout 传协议数据，日志必须写到 stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.cmd == "mcp":
        from no_agent.integrations.mcp_server import serve_stdio

        asyncio.run(serve_stdio(_agent()))
    elif args.cmd == "serve":
        import uvicorn

        from no_agent.server import create_app

        uvicorn.run(create_app(_agent()), host=args.host, port=args.port)
    elif args.cmd == "skills":
        for s in _agent().skills(include_retired=True):
            rate = f"{s.stats.successes / s.stats.runs:.0%}" if s.stats.runs else "-"
            shadow = f"  候选 v{s.shadow_version}" if s.shadow_version else ""
            print(f"{s.id:<32} {s.kind:<6} {s.status:<8} v{s.active_version}{shadow}  成功率 {rate}  {s.description[:40]}")
    elif args.cmd == "install":
        s = _agent().install_skill(args.path, overwrite=args.overwrite)
        print(f"已安装 {s.id}（{s.kind}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
