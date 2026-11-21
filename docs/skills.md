# 技能

技能是 No-Agent 沉淀经验的单位。每个技能是一个目录，全部是人类可读的文件，可以直接手改、提交到 git 审查和回滚。

## 目录格式（兼容 Anthropic Agent Skills）

**SKILL.md 是标准格式**，可以原样放进 Claude Code、Codex 等工具使用：

```
fill-form/
  SKILL.md            标准 frontmatter（name / description）+ 正文：给 AI 或人看的操作指南
  no-agent.json       本框架的扩展数据：参数 schema、版本历史、状态、权限声明
  scripts/flow_v1.py  流程脚本（可直接执行；修补后产生 flow_v2.py ...）
  tests/cases.json    回归用例（录制的工具调用与问答）
```

- 写回 SKILL.md 时只更新 `name` / `description`，**保留其他 frontmatter 字段和正文**
- 提炼新技能时，强模型会同时写出正文指南，所以学到的技能脱离本框架也能被其他 Agent 使用
- 旧格式（`manifest.json` + `vN.py`）加载时自动迁移

## 两种技能

| 类型 | 构成 | 命中后 |
|---|---|---|
| **流程型**（flow） | SKILL.md + no-agent.json + scripts | 直接执行脚本；失败时 AI 从失败步骤接管，后台修补 |
| **指南型**（guide） | 只有 SKILL.md（例如第三方的 Anthropic Skill） | 把正文注入 AI 的提示词，AI 照着做（`mode="guide"`） |

指南型技能被成功使用够多次后，会自动升级成流程型，见 [进化机制](evolution.md#技能怎么长出来)。

## 安装与挂载

```python
agent.install_skill("path/to/some-skill")          # 安装（复制到本项目，先校验 SKILL.md 和所有脚本）
AgentConfig(extra_skill_dirs=["/team/skills"])     # 或以只读方式挂载团队 / 第三方技能库（git 仓库即可）
```

共享技能库（git 仓库、签名与信任）见 [安全模型](security.md#共享技能签名与信任)。

## 显式调用

自然语言 `agent.run(...)` 每次都要先匹配技能。已经知道要用哪个技能时，直接调用更快，行为也完全可预期：

```python
run = await agent.skill("fill-form").run({"url": "https://...", "data": {"姓名": "张三"}})  # 跳过匹配，零匹配开销
run = await agent.skill("fill-form").run(task="帮我填 https://... 我叫张三")              # 只给任务：小模型抽取参数
```

`params` 会按技能的 `params_schema` 校验（必填项、类型），不合法直接抛 `ValueError`。只给 `task` 时，抽取不出参数或抽出的参数不合法也会抛 `ValueError`，不会带着错误参数开跑。

## 流程脚本怎么写

脚本由若干 `@step` 异步函数组成，按定义顺序执行；`step` 和 `ctx` 由运行时注入，无需 import。

```python
import re

async def guard(ctx):                       # 可选：在任何步骤之前确认环境符合假设（只能调用只读工具）
    return True                             # True = 继续；返回字符串 = 不适用的原因（一步都不执行，交给 AI）

@step
async def fetch(ctx):
    r = await ctx.tool("http_request", url=ctx.params["url"])
    if r["status"] != 200:
        ctx.fail(f"HTTP {r['status']}")     # 主动失败 → AI 从这一步接管
    ctx.state["html"] = r["body"]

@step
async def extract(ctx):
    m = re.search(r"<title>(.*?)</title>", ctx.state["html"], re.S | re.I)
    if not m:
        ctx.fail("页面中没有 title")
    return {"title": m.group(1).strip()}    # 最后一个非 None 的返回值是最终结果

def verify(ctx, result):                    # 强烈建议写：防止"看起来成功其实错误"
    return bool(result.get("title")) or "title 为空"
```

| ctx API | 说明 |
|---|---|
| `ctx.task` / `ctx.params` / `ctx.state` | 原始任务 / 技能参数（符合 `params_schema`）/ 步骤间共享的状态 |
| `await ctx.tool(name, **args)` | 调用工具（对外部世界的一切操作都必须经过它） |
| `await ctx.llm(instruction, input=None, json=False)` | 调小模型做"需要理解"的事（抽取、分类、总结） |
| `await ctx.ask(questions)` / `await ctx.confirm(message)` | 向用户提问（一次问完）/ 请求确认 |
| `await ctx.skill(skill_id, **params)` | 调用另一个流程型技能（见下文） |
| `ctx.log(msg)` / `ctx.fail(reason)` | 记录日志 / 主动判定失败 |

`@step(idempotent=False)` 标记有不可重复副作用的步骤。只能 import 纯计算的标准库模块，禁止 `open` / `eval` / `getattr` 和下划线开头的属性。完整规则（也是给强模型看的那一份）见 [skills/script.py](../src/no_agent/skills/script.py) 中的 `SCRIPT_API_DOC`。

确定的部分写成代码，需要理解的部分才用 `ctx.llm()`——调用点越少，脚本越确定、越省。

## 技能组合与原子技能

脚本里可以调用别的流程型技能，像调用函数一样：

```python
@step
async def report(ctx):
    page = await ctx.skill("fetch-html", url=ctx.params["url"])   # 在 fetch-html 自己的权限下运行
    ...
```

- **原子技能**（`role="atomic"`）：小粒度、可复用的积木（如登录、取网页、查订单），不直接匹配整个任务，只被其他脚本调用
- 子技能失败 → 调用它的步骤失败（父脚本整体由 AI 接管、修补）；嵌套最多 3 层，循环调用会被拦截
- 声明了权限的技能，调用子技能也要在 `permissions.skills` 中声明；子技能在自己声明的权限下运行，未声明的部分继承调用方的限制
- 提炼新技能时，强模型能看到已有的可复用技能（优先调用而不是重写），也可以顺手抽出新的原子技能
