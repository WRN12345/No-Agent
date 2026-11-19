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


class AgentConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)

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
    step_timeout: float = 60.0
    tool_timeout: float = 60.0

    # ── 进化 ──
    distill_min_traces: int = 2  # 积累多少条相似的成功轨迹后提炼新技能
    distill_similarity: float = 0.45
    patch_max_attempts: int = 2
    promote_min_pass_rate: float = 0.8  # 新版本测试通过率达到此值才自动上线
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
