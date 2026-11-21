"""
配置

作为库使用时直接构造：
    AgentConfig(llm=LLMConfig(api_key=..., fast_model=..., strong_model=...), home="./.no-agent")

也可以从环境变量读取（前缀 NO_AGENT_，嵌套用双下划线）：
    NO_AGENT_LLM__API_KEY=...  NO_AGENT_SCRIPT_ISOLATION=sandbox
    AgentConfig.from_env()

环境变量只在 from_env() 里生效（CLI 和 HTTP 服务用的就是它）；直接构造 AgentConfig(...) 时不读环境变量，
嵌入方写什么就是什么，不会被机器上的环境变量或 .env 悄悄改掉。唯一的例外是 NO_AGENT_HOME：
确定数据目录时总会参考它（见 storage/home.py）。
"""

from __future__ import annotations

from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMConfig(BaseModel):
    """LLM 配置（OpenAI 兼容协议：OpenAI / Claude / DeepSeek / Qwen / Ollama 等）

    fast：高频、便宜（技能匹配、参数抽取、脚本内的 ctx.llm）
    strong：低频、要质量（自由执行、接管、提炼、修补）
    """

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    fast_model: str = "gpt-4o-mini"
    strong_model: str = "gpt-4o"
    strong_base_url: str = ""  # 为空则沿用 base_url
    strong_api_key: str = ""  # 为空则沿用 api_key
    embed_model: str = ""  # 为空则使用词法匹配
    temperature: float | None = 0.0  # 部分推理模型不支持 temperature，设为 None
    timeout: float = 120.0


class BrowserConfig(BaseModel):
    """浏览器工具（需要 pip install "No-Agent[browser]"）"""

    enabled: bool = False
    channel: str | None = "chrome"  # 使用本机已安装的浏览器：chrome / msedge；None 用 Playwright 自带 Chromium
    headless: bool = False  # 默认显示浏览器窗口，方便看到 AI 在做什么
    slow_mo: int = 0  # 每个操作放慢的毫秒数（调试用）
    action_timeout: float = 10.0  # 单个页面操作的超时（秒）
    profile_dir: str | None = None  # 浏览器资料目录（保存登录状态等）；None 每次使用干净的临时目录

    # browser-use（需要 pip install "No-Agent[browser-use]"）：提供 browser_agent 工具，
    # 把开放式的网页子任务（登录、搜索、多页导航）交给 browser-use 在同一个浏览器里自主完成
    browser_use: bool = False
    browser_use_model: str | None = None  # 默认使用 strong 模型
    browser_use_max_steps: int = 25
    browser_use_confirm: bool = True  # 交给 browser-use 前请求确认（它的点击不经过本框架的提交确认）


class McpServerConfig(BaseModel):
    """
    要接入的外部 MCP 服务（作为客户端）。它的工具会以 "<name>__<工具名>" 注册为本地工具。
    二选一：command（本地进程，stdio）或 url（Streamable HTTP）
    """

    name: str
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    include: list[str] | None = None  # 只接入这些工具；None = 全部
    # MCP 工具的 readOnlyHint / destructiveHint 映射为本框架的 side_effect / confirm
    confirm_destructive: bool = True  # destructiveHint=true 的工具执行前请求用户确认


class SkillRepoConfig(BaseModel):
    """以只读方式使用的 git 技能仓库（No-Agent sync 拉取 / 更新）"""

    url: str
    ref: str | None = None  # 分支 / 标签；None 用默认分支
    name: str | None = None  # 本地目录名；None 由 url 推断
    subdir: str = ""  # 技能所在的子目录


class AgentConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    mcp_servers: list[McpServerConfig] = []

    # ── 存储位置（见 storage/home.py 的解析规则）──
    home: Path | None = None  # 数据根目录；为空时：环境变量 NO_AGENT_HOME > 向上查找 .no-agent/ > ./.no-agent
    skills_dir: Path | None = None  # 可写技能层；默认 <home>/skills
    extra_skill_dirs: list[Path] = []  # 只读技能层（团队共享库等），排在前面的优先级低
    skill_repos: list[SkillRepoConfig] = []  # git 技能仓库（同步到 <home>/repos/，作为只读层）
    trusted_signers: dict[str, str] = {}  # 签名者 → Ed25519 公钥（base64）
    # 共享技能（extra_skill_dirs / skill_repos）没有可信签名时：
    #   block  → 隔离：不执行、不自动使用，签名或 install 后才可用（默认，安全优先）
    #   shadow → 流程型先影子运行、比对通过后才生效（会在本进程中执行不可信脚本）；指南型不自动使用
    #   allow  → 直接信任
    untrusted_shared: Literal["block", "shadow", "allow"] = "block"
    database_url: str | None = None  # 运行记录；默认 sqlite:///<home>/runs.db，生产可用 postgresql://...
    workspace_dir: Path | None = None  # 工具工作区；默认 <home>/workspace

    # ── 匹配 ──
    match_top_k: int = 5
    match_min_score: float = 0.05  # 低于此相似度不进入候选；无候选时不调用 LLM
    match_min_confidence: float = 0.7

    # ── 执行 ──
    freeform_max_turns: int = 20
    step_timeout: float = 60.0  # 不含等待用户输入的时间
    tool_timeout: float = 60.0
    auto_confirm: bool = False  # 需确认的操作（提交等）是否跳过确认直接执行；默认每次都问

    # ── 脚本隔离（实现见 skills/process.py、skills/_worker.py、skills/_confine.py）──
    # process：在一次性子进程中执行（默认）。逃逸后拿不到密钥和宿主对象、绕不过权限，死循环可被终止；
    #          但不阻止读写本机文件和直接联网
    # sandbox：process + 操作系统级约束（Linux：Landlock 只读文件系统 + seccomp 禁止联网 / 执行程序 / 创建进程）。
    #          内核不支持时拒绝启动，不降级。untrusted_shared="shadow" 的影子运行只在这一档下执行
    # inprocess：在本进程内执行，只有 Python 层的白名单（仅用于测试或完全信任所有脚本的场景）
    script_isolation: Literal["process", "sandbox", "inprocess"] = "process"
    worker_pool_size: int = 2  # 预热的空 worker 数
    worker_memory_mb: int = 512  # 每个 worker 的内存上限；0 = 不限制
    worker_cpu_seconds: int = 300  # 每个 worker 的 CPU 时间兜底上限（宿主崩溃时也能终止失控脚本）；0 = 不限制
    script_max_tool_calls: int | None = 500  # 一次执行（含子技能）最多调用多少次工具；None = 不限制
    script_max_llm_calls: int | None = 100  # 一次执行（含子技能）最多调用多少次 ctx.llm；None = 不限制

    # ── 进化 ──
    distill_min_traces: int = 2  # 积累多少条相似的成功轨迹后提炼新技能
    distill_similarity: float = 0.45
    distill_candidates: int = 2  # 提炼时一次生成几个候选脚本，回放测试后选最好的
    patch_max_attempts: int = 2
    promote_min_pass_rate: float = 0.8  # 新版本回归测试通过率达到此值，才进入影子运行
    shadow_min_runs: int = 3  # 影子运行与真实结果一致多少次后自动转正；0 = 测试通过即上线（不做影子运行）
    shadow_max_failures: int = 1  # 影子运行失败 / 不一致超过此次数就淘汰候选版本
    max_test_cases: int = 10
    retire_window: int = 10
    retire_min_success_rate: float = 0.3

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", **overrides) -> "AgentConfig":
        """从环境变量（和可选的 .env 文件）读取配置"""

        class _Env(BaseSettings, cls):  # type: ignore[misc, valid-type]
            model_config = SettingsConfigDict(
                env_prefix="NO_AGENT_",
                env_nested_delimiter="__",
                env_file=env_file,
                env_file_encoding="utf-8",
                env_ignore_empty=True,  # .env 里留空的项视为未设置
                extra="ignore",
            )

        return cls.model_validate(_Env(**overrides).model_dump())
