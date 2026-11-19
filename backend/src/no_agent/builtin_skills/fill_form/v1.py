"""通用网页表单填写：读取全部字段 → 对应已知信息 → 一次问完缺失项 → 批量填写 → 回读校验 → 确认后提交"""

import json

SKIP_TYPES = ["file", "password"]


@step
async def open_page(ctx):
    if ctx.params.get("url"):
        await ctx.tool("browser_open", url=ctx.params["url"])


@step
async def snapshot(ctx):
    snap = await ctx.tool("browser_snapshot", scope=ctx.params.get("scope", ""))
    fields = [f for f in snap["fields"] if f["type"] not in SKIP_TYPES]
    if not fields:
        ctx.fail("页面上没有可填写的字段")
    ctx.state["fields"] = fields
    ctx.state["buttons"] = snap["buttons"]
    ctx.state["page"] = {"url": snap["url"], "title": snap["title"]}


@step(kind="llm")
async def map_values(ctx):
    fields = ctx.state["fields"]
    compact = [
        {
            "selector": f["selector"],
            "label": f["label"],
            "type": f["type"],
            "required": f["required"],
            "options": [o["label"] for o in f.get("options", [])],
        }
        for f in fields
    ]
    out = await ctx.llm(
        "把已知信息对应到表单字段。只填写有明确依据的值，不确定就不填，绝不编造。"
        "下拉框/单选用选项文字，复选框用 true/false。"
        "同时从按钮中选出提交按钮。"
        '输出 {"values": {"字段selector": 值}, "submit": "提交按钮的selector或null"}',
        {"task": ctx.task, "known": ctx.params.get("data"), "fields": compact, "buttons": ctx.state["buttons"]},
        json=True,
    )
    valid = {f["selector"] for f in fields}
    ctx.state["values"] = {k: v for k, v in (out.get("values") or {}).items() if k in valid and v not in (None, "")}
    buttons = {b["selector"] for b in ctx.state["buttons"]}
    submit = out.get("submit")
    if submit not in buttons:
        submit = next((b["selector"] for b in ctx.state["buttons"] if b.get("submit")), None)
    ctx.state["submit"] = submit


@step
async def ask_missing(ctx):
    values = ctx.state["values"]
    missing = [
        f
        for f in ctx.state["fields"]
        if f["required"] and f["selector"] not in values and f.get("value") in (None, "", False)
    ]
    if not missing:
        return
    questions = []
    for f in missing:
        q = {"key": f["selector"], "label": f["label"] or f["name"] or f["selector"], "required": True}
        if f.get("options"):
            q["kind"] = "choice"
            q["options"] = [o["label"] for o in f["options"] if o.get("value") not in (None, "")]
        elif f["type"] == "checkbox":
            q["kind"] = "confirm"
        questions.append(q)
    title = "填写「" + (ctx.state["page"]["title"] or "表单") + "」还需要以下信息"
    answers = await ctx.ask(questions, title=title)
    values.update({k: v for k, v in answers.items() if v not in (None, "")})


@step(idempotent=False)
async def fill(ctx):
    values = ctx.state["values"]
    if not values:
        ctx.fail("没有可填写的值")
    await ctx.tool("browser_fill", fields=[{"selector": k, "value": v} for k, v in values.items()])


@step
async def check(ctx):
    values = ctx.state["values"]
    got = await ctx.tool("browser_read", selectors=list(values))
    wrong = []
    for sel, want in values.items():
        have = got.get(sel)
        if isinstance(have, dict):
            ok = str(want) in (str(have.get("value")), str(have.get("label")))
        elif isinstance(have, bool):
            ok = have == (want if isinstance(want, bool) else str(want).lower() in ("true", "1", "yes", "y", "是"))
        else:
            ok = str(have) == str(want)
        if not ok:
            wrong.append({"selector": sel, "want": want, "have": have})
    if wrong:
        ctx.fail("填写后回读不一致: " + json.dumps(wrong, ensure_ascii=False))


@step(idempotent=False)
async def submit(ctx):
    labels = {f["selector"]: f["label"] or f["name"] or f["selector"] for f in ctx.state["fields"]}
    filled = {labels.get(k, k): v for k, v in ctx.state["values"].items()}
    result = {"page": ctx.state["page"], "filled": filled, "submitted": False}
    if ctx.params.get("submit", True) and ctx.state.get("submit"):
        summary = "\n".join(f"  {k}: {v}" for k, v in filled.items())
        page = await ctx.tool("browser_submit", selector=ctx.state["submit"], summary=summary)
        result["submitted"] = True
        result["after"] = {"url": page.get("url"), "title": page.get("title"), "text": (page.get("text") or "")[:300]}
    return result


def verify(ctx, result):
    if not result.get("filled"):
        return "没有填写任何字段"
    return True
