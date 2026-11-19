"""存储层：home 解析、分层技能库、写时复制、结构版本、作为库使用"""

import json

import pytest
from sqlalchemy import inspect

from no_agent import Agent, AgentConfig, FileSkillStore, Skill, SqlRunStore
from no_agent.llm import LLMs
from no_agent.storage.home import HOME_DIRNAME, init_home, resolve_home
from tests.conftest import tool_reply

SCRIPT = "@step\nasync def a(ctx):\n    return {'ok': True}\n"


def make_skill(sid="s1"):
    return Skill(id=sid, name=sid, description="d")


def test_resolve_home_order(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_AGENT_HOME", raising=False)
    project = tmp_path / "proj"
    sub = project / "src" / "pkg"
    sub.mkdir(parents=True)

    # 没有现成目录 → 当前目录
    assert resolve_home(start=sub) == sub / HOME_DIRNAME
    # 上层已有 → 向上找到（在子目录启动不会另起一份数据）
    (project / HOME_DIRNAME).mkdir()
    assert resolve_home(start=sub) == project / HOME_DIRNAME
    # 环境变量优先于查找
    monkeypatch.setenv("NO_AGENT_HOME", str(tmp_path / "env_home"))
    assert resolve_home(start=sub) == tmp_path / "env_home"
    # 显式参数最优先
    assert resolve_home(tmp_path / "explicit", start=sub) == tmp_path / "explicit"


def test_init_home_writes_gitignore(tmp_path):
    home = init_home(tmp_path / HOME_DIRNAME)
    gi = (home / ".gitignore").read_text(encoding="utf-8")
    assert "runs.db" in gi and "workspace/" in gi and "skills" not in gi.split("\n", 1)[1]


def test_layered_store_copy_on_write(tmp_path):
    shared = tmp_path / "shared"
    FileSkillStore(shared, stats_dir=tmp_path / "st0").create(make_skill("s1"), SCRIPT, reason="team")
    shared_manifest = (shared / "s1" / "manifest.json").read_text(encoding="utf-8")

    local = tmp_path / "local"
    store = FileSkillStore(local, readonly_dirs=[shared], stats_dir=tmp_path / ".stats")
    assert store.layer("s1") == "shared"

    # 统计写入不触发复制
    skill = store.get("s1")
    skill.stats.runs = 5
    store.save_stats(skill)
    assert not (local / "s1").exists()
    assert FileSkillStore(local, readonly_dirs=[shared], stats_dir=tmp_path / ".stats").get("s1").stats.runs == 5

    # 修补 → 复制到本地，只读库不变
    store.add_version("s1", SCRIPT.replace("True", "False"), reason="patch", pass_rate=1.0)
    assert store.layer("s1") == "local"
    assert (local / "s1" / "v1.py").exists() and (local / "s1" / "v2.py").exists()
    assert (shared / "s1" / "manifest.json").read_text(encoding="utf-8") == shared_manifest
    assert not (shared / "s1" / "v2.py").exists()

    # 删除本地副本 → 只读版本重新露出来；只读技能本身不能删
    store.delete("s1")
    assert store.layer("s1") == "shared" and store.get("s1").active_version == 1
    with pytest.raises(PermissionError):
        store.delete("s1")


def test_manifest_has_schema_version_and_no_stats(tmp_path):
    store = FileSkillStore(tmp_path / "skills")
    store.create(make_skill(), SCRIPT, reason="r")
    data = json.loads((tmp_path / "skills" / "s1" / "manifest.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1 and "stats" not in data


def test_future_schema_skill_is_skipped(tmp_path):
    store = FileSkillStore(tmp_path / "skills")
    store.create(make_skill(), SCRIPT, reason="r")
    p = tmp_path / "skills" / "s1" / "manifest.json"
    p.write_text(p.read_text(encoding="utf-8").replace('"schema_version": 1', '"schema_version": 99'), encoding="utf-8")
    assert FileSkillStore(tmp_path / "skills").get("s1") is None


def test_sql_tables_are_prefixed_and_versioned(tmp_path):
    store = SqlRunStore(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    names = set(inspect(store.engine).get_table_names())
    assert names == {"no_agent_runs", "no_agent_meta"}
    # 数据库版本高于框架 → 拒绝启动，避免旧版本写坏新数据
    from sqlmodel import Session

    from no_agent.storage.sql import Meta

    with Session(store.engine) as s:
        s.merge(Meta(key="schema_version", value="99"))
        s.commit()
    with pytest.raises(RuntimeError, match="高于"):
        SqlRunStore(engine=store.engine)


def test_agent_as_library(tmp_path, fast, strong, web):
    """嵌入使用：只配 home，数据都落在 home 下；run_sync 可在同步代码中使用"""
    web.pages = {"http://site/1": "<title>X</title>"}
    strong.on(
        "freeform",
        lambda m, t: tool_reply("finish", {"success": True, "result": "X"}, "c2")
        if m[-1]["role"] == "tool"
        else tool_reply("fetch_page", {"url": "http://site/1"}),
    )
    home = tmp_path / "proj" / HOME_DIRNAME
    agent = Agent(AgentConfig(home=home), tools=[web.tool()], llms=LLMs(fast=fast, strong=strong))
    run = agent.run_sync("获取 http://site/1 的标题")
    assert run.success and run.mode == "freeform"
    assert {p.name for p in home.iterdir()} >= {"skills", "runs.db", "workspace", ".stats", ".gitignore"}
    assert "fetch_page" in agent.registry.names() and "http_request" in agent.registry.names()
    assert agent.evolver.history[-1]["status"] == "waiting"  # run_sync 等待了后台进化任务
