# 浏览器与人机交互

## 浏览器

需要 `pip install "No-Agent[browser]"`。不走"截图 → 看图 → 移鼠标"的 computer use，而是读元素树、批量操作：

```python
from no_agent import Agent, AgentConfig, BrowserConfig

agent = Agent(AgentConfig(browser=BrowserConfig(enabled=True)))   # 框架自己启动本机 Chrome
async with agent:
    run = await agent.run("在 https://example.com/apply 填写报名表，我叫张三，技术部")
```

运行示例：`python examples/fill_form.py <网址> "要填写的信息"`（[examples/fill_form.py](../examples/fill_form.py)）

**浏览器工具**：`browser_open` / `browser_snapshot` / `browser_fill` / `browser_read` / `browser_click` / `browser_submit` / `browser_text`（[tools/browser.py](../src/no_agent/tools/browser.py)）。

- 提交按钮只能通过 `browser_submit`（需要确认），`browser_click` 会拒绝点击提交按钮
- 浏览器工具都标注为 `stateful`：后台测试 / 影子运行时只用录制结果，不会操作你正在用的浏览器

配置项见 `BrowserConfig`（[config.py](../src/no_agent/config.py)）：`channel`（chrome / msedge）、`headless`、`profile_dir`（保存登录状态）等。

### 内置 `fill-form` 技能

开启浏览器后自动加载，可被修补、可在本地覆盖（[builtin_skills/fill-form](../src/no_agent/builtin_skills/fill-form)）：

```
open_page → snapshot（一次读出全部字段：标签/类型/必填/选项/稳定选择器）
          → map_values（小模型把已知信息对应到字段，只填有依据的）
          → ask_missing（缺失的必填项在终端里一次问完）
          → fill（批量填写）→ check（回读校验，不一致就失败、交给 AI 接管）
          → submit（框架强制请求确认，确认内容里带填写摘要）
```

### browser-use（可选）

需要 `pip install "No-Agent[browser-use]"`。`BrowserConfig(browser_use=True)` 后多一个 `browser_agent` 工具：把开放式的网页子任务（登录、搜索、翻页、在菜单里找页面）交给 browser-use，在**同一个浏览器**里通过 CDP 完成；完成后 `browser_snapshot` / `browser_fill` 等确定性工具在它停下的页面上接着操作。

- 分工：browser-use 负责"把页面带到正确位置"（每步都要大模型推理，贵、不确定）；可重复的部分交给确定性工具，才能沉淀成省 token 的技能
- 安全：browser-use 的点击不经过本框架的提交确认，所以**调用 browser_agent 本身默认需要用户确认**，并要求它不要提交、付款、删除
- browser-use 内部的每一步不会录进轨迹，经过它的部分无法被提炼成确定性脚本（只能提炼它前后的确定性步骤）
- 已在 browser-use 1.0.6 上验证接入与断开（断开后浏览器和页面保持可用）

## 提问与确认

- 脚本里用 `await ctx.ask([...])` / `await ctx.confirm("...")`，AI 自由执行时用 `ask_user` 工具，都要求**一次问完**
- 工具声明 `confirm=True`（如 `browser_submit`）后，执行前由框架强制确认，不依赖脚本或模型自觉；默认每次都问，`auto_confirm=True` 可关闭
- 用户拒绝确认或输入 `:q` 取消 → `mode="aborted"`：不算脚本失败，不触发接管 / 修补，不计入技能统计
- 等待用户输入的时间不计入步骤超时
- 问答会录进轨迹，提炼和测试回放时直接用录制的回答，不会再弹问题，也不会真的提交
- 提问方式可替换：`Agent(asker=...)`，实现 `ask()` / `confirm()` 即可（[interact.py](../src/no_agent/interact.py)）。默认在交互式终端中提问；非交互环境为 None，此时需确认的操作一律拒绝。`ScriptedAsker` 用于测试和无人值守的批处理
- 作为 MCP 服务运行时，提问和确认变成 MCP elicitation，见 [MCP](mcp.md)
