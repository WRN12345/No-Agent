"""阶段 1：SKILL.md 格式、旧格式迁移、第三方指南型技能、安装、显式调用"""

import json

import pytest

from no_agent import FileSkillStore, Skill
from no_agent.skills import skillmd
from tests.conftest import json_reply, tool_reply
from tests.test_evolution import SKILL_V1

URL_SCHEMA = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

THIRD_PARTY = """---
name: weekly-report
description: 按公司模板撰写周报。用户要写周报、总结本周工作时使用。
license: MIT
allowed-tools: Read Write
---

# 写周报

1. 先读 `notes/` 下本周的笔记
2. 按「本周完成 / 下周计划 / 风险」三段写
3. 每段不超过 5 条
"""


def write_legacy(root, sid="page_title"):
    d = root / sid
    d.mkdir(parents=True)
    manifest = Skill(id=sid, name="网页标题", description="获取网页标题", params_schema=URL_SCHEMA).model_dump(mode="json")
    manifest.update(schema_version=1, active_version=1, versions=[{"version": 1, "reason": "old"}])
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (d / "v1.py").write_text(SKILL_V1, encoding="utf-8")
    return d


# ── 旧格式迁移 ──


def test_legacy_skill_migrated_in_writable_layer(tmp_path):
    root, stats = tmp_path / "skills", tmp_path / ".stats"
    write_legacy(root)
    stats.mkdir()
    (stats / "page_title.json").write_text('{"runs": 7}', encoding="utf-8")

    store = FileSkillStore(root, stats_dir=stats)
    skill = store.get("page-title")
    assert skill and skill.kind == "flow" and skill.stats.runs == 7
    d = root / "page-title"
    assert sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) == ["SKILL.md", "no-agent.json", "scripts/flow_v1.py"]
    assert not (root / "page_title").exists() and (stats / "page-title.json").exists()
    assert store.script("page-title") == SKILL_V1
    assert FileSkillStore(root, stats_dir=stats).get("page-title").stats.runs == 7  # 迁移后再次加载稳定


def test_legacy_skill_in_readonly_layer_is_not_written(tmp_path):
    shared = tmp_path / "shared"
    d = write_legacy(shared)
    before = sorted(p.name for p in d.iterdir())
    store = FileSkillStore(tmp_path / "local", readonly_dirs=[shared], stats_dir=tmp_path / ".stats")
    assert store.script("page-title") == SKILL_V1 and store.layer("page-title") == "shared"
    assert sorted(p.name for p in d.iterdir()) == before  # 只读层原样不动

    store.add_version("page-title", SKILL_V1 + "\n", reason="patch", pass_rate=1.0)  # 写时复制 + 本地迁移
    local = tmp_path / "local" / "page-title"
    assert (local / "SKILL.md").exists() and (local / "scripts" / "flow_v2.py").exists() and not (local / "manifest.json").exists()
    assert sorted(p.name for p in d.iterdir()) == before


# ── 第三方指南型技能（纯 SKILL.md） ──


@pytest.fixture
def third_party(tmp_path):
    d = tmp_path / "downloads" / "weekly-report"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(THIRD_PARTY, encoding="utf-8")
    return d


def test_guide_skill_loaded_and_run_with_guide_in_prompt(agent, fast, strong, third_party):
    skill = agent.install_skill(third_party)
    assert skill.kind == "guide" and skill.id == "weekly-report" and "三段写" in skill.guide

    fast.on("match", lambda m, t: json_reply({"skill_id": "weekly-report", "confidence": 0.9, "params": {}}))
    seen = {}

    def guide(messages, tools):
        seen["system"] = messages[0]["content"]
        return tool_reply("finish", {"success": True, "result": "本周完成：..."})

    strong.on("guide", guide)
    run = agent.run_sync("帮我写这周的周报")
    assert run.mode == "guide" and run.success and run.skill_id == "weekly-report"
    assert "参考技能：weekly-report" in seen["system"] and "本周完成 / 下周计划 / 风险" in seen["system"]
    assert agent.store.get("weekly-report").stats.successes == 1  # 指南型按最终成功计


def test_saving_keeps_extra_frontmatter_and_body(agent, third_party):
    agent.install_skill(third_party)
    agent.store.set_status("weekly-report", "retired")
    meta, body = skillmd.parse((agent.store.path("weekly-report") / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["license"] == "MIT" and meta["allowed-tools"] == "Read Write"
    assert body == skillmd.parse(THIRD_PARTY)[1]
    assert agent.store.get("weekly-report").status == "retired"


def test_install_validates(agent, tmp_path, third_party):
    bad_name = tmp_path / "bad"
    bad_name.mkdir()
    (bad_name / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: x\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        agent.install_skill(bad_name)

    bad_script = tmp_path / "bad-script"
    (bad_script / "scripts").mkdir(parents=True)
    (bad_script / "SKILL.md").write_text("---\nname: bad-script\ndescription: x\n---\nbody", encoding="utf-8")
    (bad_script / "no-agent.json").write_text(json.dumps({"name": "x", "active_version": 1, "versions": [{"version": 1}]}), encoding="utf-8")
    (bad_script / "scripts" / "flow_v1.py").write_text("import os\n@step\nasync def a(ctx):\n    pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="import os"):
        agent.install_skill(bad_script)

    with pytest.raises(ValueError, match="不是技能目录"):
        agent.install_skill(tmp_path)

    agent.install_skill(third_party)
    with pytest.raises(FileExistsError):
        agent.install_skill(third_party)
    agent.install_skill(third_party, overwrite=True)


# ── 显式调用 ──


@pytest.fixture
def page_title(agent):
    agent.store.create(
        Skill(id="page-title", name="网页标题", description="获取网页标题", params_schema=URL_SCHEMA), SKILL_V1, reason="t"
    )
    return agent.skill("page-title")


def test_explicit_call_skips_matching(agent, fast, strong, web, page_title):
    web.pages = {"http://site/1": "<title>Hello</title>"}
    run = page_title.run_sync({"url": "http://site/1"})
    assert run.mode == "skill" and run.result == {"title": "Hello"}
    assert fast.calls == [] and strong.calls == []  # 零 LLM 调用
    assert run.prompt_tokens + run.completion_tokens == 0


async def test_explicit_call_validates_params(agent, page_title):
    with pytest.raises(ValueError, match="缺少必填参数 url"):
        await page_title.run({})
    with pytest.raises(ValueError, match="应为 string"):
        await page_title.run({"url": 123})
    with pytest.raises(KeyError):
        agent.skill("nope")


def test_explicit_call_with_task_extracts_params(agent, fast, web, page_title):
    web.pages = {"http://site/2": "<title>Two</title>"}
    fast.on("match", lambda m, t: json_reply({"skill_id": "page-title", "confidence": 0.9, "params": {"url": "http://site/2"}}))
    run = page_title.run_sync(task="看看 http://site/2 的标题")
    assert run.result == {"title": "Two"} and fast.calls == ["match"]


def test_api_explicit_run_and_kind(agent, web, page_title, third_party):
    from fastapi.testclient import TestClient

    from no_agent.server import create_app

    agent.install_skill(third_party)
    web.pages = {"http://site/1": "<title>API</title>"}
    with TestClient(create_app(agent), client=("127.0.0.1", 50000)) as client:
        kinds = {s["id"]: (s["kind"], s["layer"]) for s in client.get("/api/v1/skills").json()}
        assert kinds == {"page-title": ("flow", "local"), "weekly-report": ("guide", "local")}
        r = client.post("/api/v1/skills/page-title/run", json={"params": {"url": "http://site/1"}})
        assert r.status_code == 200 and r.json()["result"] == {"title": "API"}
        assert client.post("/api/v1/skills/page-title/run", json={"params": {}}).status_code == 422
        detail = client.get("/api/v1/skills/weekly-report").json()
        assert detail["kind"] == "guide" and "script" not in detail and "三段写" in detail["guide"]
