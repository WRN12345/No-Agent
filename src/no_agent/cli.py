"""
命令行入口（配置从 NO_AGENT_* 环境变量 / .env 读取）

    no-agent mcp                启动 MCP 服务（stdio），供 Claude Desktop / Claude Code / Cursor 等接入
    no-agent serve [--port N]   启动 HTTP 服务
    no-agent skills             列出技能
    no-agent install <目录>     安装技能（SKILL.md 目录）
    no-agent sync               拉取 / 更新 git 技能仓库
    no-agent keygen / sign      生成签名密钥 / 给技能签名

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
    sub.add_parser("sync", help="拉取 / 更新配置的 git 技能仓库")
    p_keygen = sub.add_parser("keygen", help="生成签名密钥")
    p_keygen.add_argument("--out", required=True, help="私钥保存路径（妥善保管，不要提交到 git）")
    p_sign = sub.add_parser("sign", help="给技能目录签名")
    p_sign.add_argument("path")
    p_sign.add_argument("--key", required=True, help="私钥文件")
    p_sign.add_argument("--signer", required=True, help="签名者名称（与 trusted_signers 中的键一致）")
    p_reviews = sub.add_parser("reviews", help="列出等待审查的版本（扩大了能力，没有自动上线）")
    p_reviews.add_argument("--diff", action="store_true", help="同时显示脚本 diff")
    p_approve = sub.add_parser("approve", help="批准等待审查的版本")
    p_approve.add_argument("skill_id")
    p_approve.add_argument("version", type=int)
    p_approve.add_argument("--comment", default="")
    p_reject = sub.add_parser("reject", help="拒绝等待审查的版本")
    p_reject.add_argument("skill_id")
    p_reject.add_argument("version", type=int)
    p_reject.add_argument("--reason", default="审查未通过")
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
    elif args.cmd == "reviews":
        items = _agent().reviews()
        if not items:
            print("没有等待审查的版本")
        for r in items:
            rv = r["review"]
            print(f"{r['skill_id']} v{r['version']}（{rv['kind']}）: {r['reason']}")
            for reason in rv["reasons"]:
                print(f"  - {reason}")
            if rv["added_imports"]:
                print(f"  · 新增 import: {', '.join(rv['added_imports'])}")
            if rv["llm_calls_before"] is not None and rv["llm_calls_after"] != rv["llm_calls_before"]:
                print(f"  · ctx.llm 调用点: {rv['llm_calls_before']} → {rv['llm_calls_after']}")
            if rv["atoms"]:
                print(f"  · 一起新建的原子技能: {', '.join(rv['atoms'])}")
            if args.diff:
                print(r["diff"])
    elif args.cmd in ("approve", "reject"):
        a = _agent()
        try:
            if args.cmd == "approve":
                s = a.approve(args.skill_id, args.version, args.comment)
            else:
                s = a.reject(args.skill_id, args.version, args.reason)
        except (ValueError, KeyError) as e:
            print(f"失败: {e}", file=sys.stderr)
            return 1
        shadow = f"，候选 v{s.shadow_version} 进入影子运行" if s.shadow_version else ""
        print(f"{'已批准' if args.cmd == 'approve' else '已拒绝'} {s.id} v{args.version}：状态 {s.status}，当前版本 v{s.active_version}{shadow}")
    elif args.cmd == "install":
        s = _agent().install_skill(args.path, overwrite=args.overwrite)
        print(f"已安装 {s.id}（{s.kind}）")
    elif args.cmd == "sync":
        for r in asyncio.run(_agent().sync_skill_repos()):
            print(f"{r['url']}: {r.get('commit') or '失败 ' + r.get('error', '')}")
    elif args.cmd == "keygen":
        from pathlib import Path

        from no_agent.skills.signing import generate_keypair

        pem, pub = generate_keypair()
        Path(args.out).write_bytes(pem)
        print(f"私钥已保存到 {args.out}\n公钥（填入 trusted_signers）: {pub}")
    elif args.cmd == "sign":
        from pathlib import Path

        from no_agent.skills.signing import sign_skill

        sig = sign_skill(Path(args.path), Path(args.key).read_bytes(), args.signer)
        print(f"已签名 {sig['skill']}：{len(sig['files'])} 个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
