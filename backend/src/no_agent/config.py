"""
配置

作为库使用时直接构造：
    AgentConfig(llm=LLMConfig(api_key=..., fast_model=..., strong_model=...), home="./.no-agent")

也可以从环境变量读取（前缀 NO_AGENT_，嵌套用双下划线）：
    NO_AGENT_LLM__API_KEY=...  NO_AGENT_HOME=...
    AgentConfig.from_env()
"""

from __future__ import annotations

from pathlib import Path

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


class AgentConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)

    # ── 存储位置（见 storage/home.py 的解析规则）──
    home: Path | None = None  # 数据根目录；为空时：环境变量 NO_AGENT_HOME > 向上查找 .no-agent/ > ./.no-agent
    skills_dir: Path | None = None  # 可写技能层；默认 <home>/skills
    extra_skill_dirs: list[Path] = []  # 只读技能层（团队共享库等），排在前面的优先级低
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

    # ── 进化 ──
    distill_min_traces: int = 2  # 积累多少条相似的成功轨迹后提炼新技能
    distill_similarity: float = 0.45
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
