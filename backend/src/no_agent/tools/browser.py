"""
浏览器工具（Playwright，由框架自己启动浏览器）

与"截图 + 鼠标"式的 computer use 不同：
- browser_snapshot 一次读出页面上所有可填写的字段（标签、类型、必填、选项、当前值）
  和按钮，每个元素都带稳定的 CSS 选择器（优先 id / name / aria-label）
- browser_fill 一次批量填写
→ 一次理解、一次填写，token 少；稳定的选择器可以沉淀进技能脚本反复使用

需要 pip install "No-Agent[browser]"。默认使用本机已安装的 Chrome（channel="chrome"），
无需额外下载浏览器；没有 Chrome 时可设 channel=None 并运行 `playwright install chromium`。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from no_agent.tools.base import Tool, ToolError, tool

if TYPE_CHECKING:
    from no_agent.config import BrowserConfig

logger = logging.getLogger(__name__)

_SNAPSHOT_JS = r"""
(scope) => {
  const root = scope ? document.querySelector(scope) : document.body;
  if (!root) return {error: `找不到范围 ${scope}`};
  const esc = s => CSS.escape(s);
  const uniq = sel => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const visible = el => {
    const st = getComputedStyle(el), r = el.getBoundingClientRect();
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    return r.width > 0 || r.height > 0 || el.type === 'radio' || el.type === 'checkbox';
  };
  const text = el => el ? (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80) : '';
  const labelOf = el => {
    const aria = el.getAttribute('aria-label'); if (aria) return aria;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => text(document.getElementById(id))).join(' ').trim(); if (t) return t; }
    if (el.id) { const l = document.querySelector(`label[for="${esc(el.id)}"]`); if (l) return text(l); }
    const wrap = el.closest('label');
    if (wrap) {  // 包裹式 label：去掉控件自身的文字（如下拉框的选项）
      const c = wrap.cloneNode(true);
      c.querySelectorAll('select, textarea, input, button').forEach(n => n.remove());
      const t = (c.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80);
      if (t) return t;
    }
    return el.placeholder || el.title || el.name || '';
  };
  const attr = (tag, k, v) => `${tag}[${k}="${String(v).replace(/"/g, '\\"')}"]`;
  const selectorOf = el => {
    const tag = el.tagName.toLowerCase();
    if (el.id && uniq('#' + esc(el.id))) return '#' + esc(el.id);
    if (el.name) { const s = attr(tag, 'name', el.name); if (uniq(s)) return s; }
    const al = el.getAttribute('aria-label'); if (al) { const s = attr(tag, 'aria-label', al); if (uniq(s)) return s; }
    const parts = []; let e = el;
    while (e && e.nodeType === 1 && e !== document.body) {
      if (e !== el && e.id && uniq('#' + esc(e.id))) { parts.unshift('#' + esc(e.id)); break; }
      let i = 1, s = e; while ((s = s.previousElementSibling)) if (s.tagName === e.tagName) i++;
      parts.unshift(`${e.tagName.toLowerCase()}:nth-of-type(${i})`); e = e.parentElement;
    }
    return parts.join(' > ');
  };
  const fields = [], buttons = [], radios = {};
  for (const el of root.querySelectorAll('input, select, textarea, button, [contenteditable="true"]')) {
    if (el.disabled || !visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (tag === 'button' || ['submit', 'button', 'reset', 'image'].includes(type)) {
      const isSubmit = type === 'submit' || (tag === 'button' && !el.getAttribute('type') && !!el.form);
      buttons.push({selector: selectorOf(el), text: text(el) || el.value || '', submit: isSubmit});
      continue;
    }
    if (type === 'hidden') continue;
    if (type === 'radio') {
      let g = radios[el.name];
      if (!g) {
        const fs = el.closest('fieldset'), lg = fs && fs.querySelector('legend');
        g = radios[el.name] = {selector: `input[type="radio"][name="${el.name}"]`, label: lg ? text(lg) : el.name,
                               type: 'radio', name: el.name, required: false, value: null, options: []};
        fields.push(g);
      }
      g.required = g.required || el.required;
      if (el.checked) g.value = el.value;
      g.options.push({value: el.value, label: labelOf(el)});
      continue;
    }
    const f = {selector: selectorOf(el), label: labelOf(el), type, name: el.name || '',
               required: !!el.required || el.getAttribute('aria-required') === 'true'};
    if (tag === 'select') { f.type = 'select'; f.options = [...el.options].map(o => ({value: o.value, label: text(o)})); f.value = el.value; }
    else if (type === 'checkbox') f.value = el.checked;
    else if (el.isContentEditable && tag !== 'input' && tag !== 'textarea') { f.type = 'contenteditable'; f.value = text(el); }
    else f.value = el.value;
    if (el.placeholder) f.placeholder = el.placeholder;
    fields.push(f);
  }
  return {url: location.href, title: document.title, fields, buttons};
}
"""

_KIND_JS = r"""
els => {
  const el = els[0];
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'select') return 'select';
  if (type === 'checkbox') return 'checkbox';
  if (type === 'radio') return 'radio';
  if (tag === 'button' || type === 'submit') return (type === 'submit' || (tag === 'button' && !el.getAttribute('type') && el.form)) ? 'submit' : 'button';
  return 'text';
}
"""

_READ_JS = r"""
els => {
  const el = els[0];
  if (!el) return {missing: true};
  const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'select') { const o = el.options[el.selectedIndex]; return {value: el.value, label: o ? o.text.trim() : ''}; }
  if (type === 'checkbox') return {value: el.checked};
  if (type === 'radio') { const c = els.find(e => e.checked); return {value: c ? c.value : null}; }
  if (el.isContentEditable && tag !== 'input' && tag !== 'textarea') return {value: el.innerText.trim()};
  return {value: el.value};
}
"""

_PICK_RADIO_JS = r"""
(els, want) => {
  const norm = s => String(s ?? '').trim().toLowerCase();
  const label = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText; }
    const w = el.closest('label'); return w ? w.innerText : '';
  };
  return els.findIndex(e => norm(e.value) === norm(want) || norm(label(e)) === norm(want));
}
"""

_TRUE = {"true", "1", "yes", "y", "on", "checked", "是", "勾选", "同意", "选中"}


def _truthy(v: Any) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in _TRUE


class BrowserSession:
    """懒启动的浏览器会话，整个 Agent 共用一个页面"""

    def __init__(self, cfg: BrowserConfig) -> None:
        self.cfg = cfg
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._lock: asyncio.Lock | None = None

    async def page(self) -> Any:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise ToolError('浏览器工具需要额外依赖：pip install "No-Agent[browser]"') from e
            if self._pw is None:
                self._pw = await async_playwright().start()
            if self._browser is None or not self._browser.is_connected():
                self._browser = await self._launch()
            context = await self._browser.new_context()
            context.set_default_timeout(self.cfg.action_timeout * 1000)
            self._page = await context.new_page()
            return self._page

    async def _launch(self) -> Any:
        kw = {"headless": self.cfg.headless, "slow_mo": self.cfg.slow_mo}
        try:
            return await self._pw.chromium.launch(channel=self.cfg.channel, **kw) if self.cfg.channel else await self._pw.chromium.launch(**kw)
        except Exception as e:  # noqa: BLE001
            if not self.cfg.channel:
                raise ToolError(f"无法启动浏览器: {e}（可运行 `playwright install chromium`）") from e
            logger.warning("无法启动本机 %s（%s），改用 Playwright 自带 Chromium", self.cfg.channel, e)
            try:
                return await self._pw.chromium.launch(**kw)
            except Exception as e2:  # noqa: BLE001
                raise ToolError(f"无法启动浏览器: {e2}（请安装 Chrome，或运行 `playwright install chromium`）") from e2

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            logger.debug("关闭浏览器出错", exc_info=True)
        finally:
            self._pw = self._browser = self._page = None
            self._lock = None


async def _page_info(page: Any) -> dict[str, Any]:
    return {"url": page.url, "title": await page.title()}


async def _settle(page: Any) -> None:
    """等待操作引起的导航 / 渲染稳定（不会因为页面一直有请求而卡住）"""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(200)


def make_browser_tools(session: BrowserSession) -> list[Tool]:
    @tool(
        "browser_open",
        url_arg="url",
        description="在浏览器中打开网址",
        parameters={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    )
    async def browser_open(url: str) -> dict[str, Any]:
        page = await session.page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"打开失败: {e}") from e
        return await _page_info(page)

    @tool(
        "browser_snapshot",
        description=(
            "读取当前页面上所有可填写的字段和按钮（一次拿全）。每个字段包含 selector、label、type、required、"
            "options、当前 value；按钮包含 selector、text、submit（是否是提交按钮）。scope 可限定在某个容器内。"
        ),
        parameters={"type": "object", "properties": {"scope": {"type": "string", "description": "可选，CSS 选择器"}}},
    )
    async def browser_snapshot(scope: str = "") -> dict[str, Any]:
        page = await session.page()
        snap = await page.evaluate(_SNAPSHOT_JS, scope or None)
        if snap.get("error"):
            raise ToolError(snap["error"])
        return snap

    @tool(
        "browser_fill",
        description=(
            "批量填写表单字段：fields=[{selector, value}]。文本框直接填；下拉框 value 可以是选项值或选项文字；"
            "复选框用 true/false；单选组用选项值或选项文字。有字段填写失败时报错并列出失败的字段。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"selector": {"type": "string"}, "value": {}}, "required": ["selector", "value"]},
                }
            },
            "required": ["fields"],
        },
        side_effect=True,
    )
    async def browser_fill(fields: list[dict[str, Any]]) -> dict[str, Any]:
        page = await session.page()
        filled, errors = [], []
        for f in fields:
            sel, val = f.get("selector", ""), f.get("value")
            try:
                loc = page.locator(sel)
                kind = await loc.evaluate_all(_KIND_JS)
                if kind is None:
                    raise ToolError("找不到元素")
                if kind == "select":
                    try:
                        await loc.first.select_option(value=str(val))
                    except Exception:  # noqa: BLE001
                        await loc.first.select_option(label=str(val))
                elif kind == "checkbox":
                    await loc.first.set_checked(_truthy(val))
                elif kind == "radio":
                    idx = await loc.evaluate_all(_PICK_RADIO_JS, val)
                    if idx < 0:
                        raise ToolError(f"没有选项 {val!r}")
                    await loc.nth(idx).check()
                elif kind in ("submit", "button"):
                    raise ToolError("这是按钮，不能填写")
                else:
                    await loc.first.fill("" if val is None else str(val))
                filled.append(sel)
            except Exception as e:  # noqa: BLE001
                msg = str(e).splitlines()[0][:200]
                errors.append({"selector": sel, "error": msg})
        if errors:
            raise ToolError("以下字段填写失败: " + json.dumps(errors, ensure_ascii=False) + f"（已成功 {len(filled)} 个）")
        return {"filled": len(filled)}

    @tool(
        "browser_read",
        description="读取字段的当前值：selectors=[...]，返回 {selector: value}（下拉框返回 {value, label}）",
        parameters={
            "type": "object",
            "properties": {"selectors": {"type": "array", "items": {"type": "string"}}},
            "required": ["selectors"],
        },
    )
    async def browser_read(selectors: list[str]) -> dict[str, Any]:
        page = await session.page()
        out: dict[str, Any] = {}
        for sel in selectors:
            r = await page.locator(sel).evaluate_all(_READ_JS)
            out[sel] = None if r.get("missing") else (r if "label" in r else r["value"])
        return out

    @tool(
        "browser_click",
        description="点击普通按钮 / 链接（不能用于提交表单，提交请用 browser_submit）",
        parameters={"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]},
        side_effect=True,
    )
    async def browser_click(selector: str) -> dict[str, Any]:
        page = await session.page()
        loc = page.locator(selector)
        kind = await loc.evaluate_all(_KIND_JS)
        if kind is None:
            raise ToolError(f"找不到元素 {selector}")
        if kind == "submit":
            raise ToolError("这是提交按钮，请使用 browser_submit")
        await loc.first.click()
        await _settle(page)
        return await _page_info(page)

    @tool(
        "browser_submit",
        description="点击提交按钮提交表单。执行前系统会自动请求用户确认；summary 是展示给用户确认的内容摘要（例如填了哪些值）",
        parameters={
            "type": "object",
            "properties": {"selector": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["selector"],
        },
        side_effect=True,
        confirm=True,
        confirm_message=lambda a: "即将提交表单" + (f"：\n{a['summary']}" if a.get("summary") else f"（{a.get('selector')}）"),
    )
    async def browser_submit(selector: str, summary: str = "") -> dict[str, Any]:
        page = await session.page()
        loc = page.locator(selector)
        if await loc.count() == 0:
            raise ToolError(f"找不到提交按钮 {selector}")
        await loc.first.click()
        await _settle(page)
        info = await _page_info(page)
        info["text"] = (await page.inner_text("body"))[:1500]
        return info

    @tool(
        "browser_text",
        description="读取页面（或某个元素）的可见文字，用于确认结果",
        parameters={
            "type": "object",
            "properties": {"selector": {"type": "string", "default": "body"}, "max_chars": {"type": "integer", "default": 4000}},
        },
    )
    async def browser_text(selector: str = "body", max_chars: int = 4000) -> str:
        page = await session.page()
        try:
            return (await page.inner_text(selector))[:max_chars]
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"读取失败: {str(e).splitlines()[0]}") from e

    return [browser_open, browser_snapshot, browser_fill, browser_read, browser_click, browser_submit, browser_text]
