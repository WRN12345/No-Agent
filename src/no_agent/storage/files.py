"""
文件化技能库（分层，SKILL.md 格式）

  只读层（extra_skill_dirs）：团队共享 / 随包分发 / 第三方的技能库，框架不会写入
  可写层（skills_dir）：      本项目学到的技能，以及从只读层"写时复制"过来并修改过的技能

同 id 时可写层覆盖只读层。只读层的技能被修补 / 切换版本 / 下线时，先整体复制到可写层再修改。

每个技能一个目录（目录名 = 技能 id = SKILL.md 的 name）：
  SKILL.md           标准格式：name / description + 正文指南
  no-agent.json      扩展数据：参数、版本、状态、权限（没有它就是纯指南型技能，如第三方 Anthropic Skills）
  scripts/flow_vN.py 各版本流程脚本
  tests/cases.json   回归用例
运行统计（高频变化的运行时数据）单独存在 <home>/.stats/<id>.json，不进 git，也不会触发写时复制。

旧格式（schema_version 1：manifest.json + vN.py）加载时自动迁移；在可写层会就地改写成新格式。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from no_agent.skills import skillmd
from no_agent.skills.models import ShadowState, Skill, SkillStats, TestCase, VersionInfo
from no_agent.skills.script import ScriptError, script_tools, step_docs, validate_script
from no_agent.storage.base import SKILL_SCHEMA_VERSION as SCHEMA_VERSION
from no_agent.tools.base import ALWAYS_ALLOWED

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
EXT_FILE = "no-agent.json"
TESTS_FILE = "tests/cases.json"
LEGACY_MANIFEST = "manifest.json"

# 只属于 SKILL.md 或单独文件的字段，不写进 no-agent.json
_EXT_EXCLUDE = {"id", "description", "guide", "tests", "stats"}


def slugify(text: str) -> str:
    """转为合法的技能 id：小写字母、数字、连字符，≤64"""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[: skillmd.NAME_MAX].strip("-") or "skill"


def script_relpath(version: int) -> str:
    return f"scripts/flow_v{version}.py"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_skill_dir(d: Path) -> Skill | None:
    """从目录读取技能（只读，不做任何写入）；不是技能目录返回 None"""
    if (d / SKILL_FILE).is_file():
        meta, body = skillmd.parse((d / SKILL_FILE).read_text(encoding="utf-8"))
        ext: dict[str, Any] = {}
        if (d / EXT_FILE).is_file():
            ext = json.loads((d / EXT_FILE).read_text(encoding="utf-8"))
        if int(ext.get("schema_version", SCHEMA_VERSION)) > SCHEMA_VERSION:
            raise ValueError(f"结构版本 {ext['schema_version']} 高于当前框架支持的 {SCHEMA_VERSION}")
        sid = str(meta.get("name") or d.name)
        data = {
            **{k: v for k, v in ext.items() if k not in _EXT_EXCLUDE},
            "id": sid,
            "name": ext.get("name") or sid,
            "description": str(meta.get("description") or ""),
            "guide": body,
        }
        if not ext:
            data["active_version"] = 0  # 纯 SKILL.md：指南型
        skill = Skill.model_validate(data)
        if (d / TESTS_FILE).is_file():
            skill.tests = [TestCase.model_validate(t) for t in json.loads((d / TESTS_FILE).read_text(encoding="utf-8"))]
        return skill
    if (d / LEGACY_MANIFEST).is_file():
        return _legacy_skill(d)
    return None


def _legacy_skill(d: Path) -> Skill:
    """schema_version 1：manifest.json + vN.py"""
    data = json.loads((d / LEGACY_MANIFEST).read_text(encoding="utf-8"))
    if int(data.get("schema_version", 1)) > 1:
        raise ValueError("manifest.json 的结构版本未知")
    data.pop("stats", None)
    data["schema_version"] = SCHEMA_VERSION
    data["id"] = slugify(data["id"])
    data.setdefault("active_version", 1)
    return Skill.model_validate(data)


class FileSkillStore:
    def __init__(
        self,
        root: Path,
        *,
        readonly_dirs: list[Path] | None = None,
        stats_dir: Path | None = None,
        trusted_dirs: list[Path] | None = None,
        verifier: Callable[[Path], tuple[bool, str]] | None = None,
        untrusted_mode: Literal["block", "shadow"] = "block",
    ) -> None:
        """
        trusted_dirs：无需验证签名的只读层（如框架内置技能）
        verifier：只读层技能的信任检查（返回 (是否可信, 说明)）。不可信的技能一律不对外生效，
          也不能被其他技能 ctx.skill() 调用；需要签名或 install 显式信任。
        untrusted_mode：不可信流程型技能的处理
          block  → 完全不执行（默认）。脚本在进程内执行，沙箱不是强安全边界，执行即等于信任
          shadow → 在本地任务上影子运行、比对通过后转正（会在本进程中执行不可信代码，仅适用于来源基本可信的场景）
        verifier 为 None 时只读层全部视为可信
        """
        self.root = Path(root)
        self.readonly_dirs = [Path(d) for d in (readonly_dirs or [])]
        self.trusted_dirs = {Path(d).resolve() for d in (trusted_dirs or [])}
        self.verifier = verifier
        self.trust: dict[str, str] = {}  # 只读层技能的信任说明
        self.untrusted: set[str] = set()  # 未通过信任检查的只读层技能
        self.untrusted_mode = untrusted_mode
        self.stats_dir = Path(stats_dir) if stats_dir else self.root.parent / ".stats"
        self.root.mkdir(parents=True, exist_ok=True)
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, Skill] = {}
        self._origin: dict[str, Path] = {}  # 技能所在目录
        self._legacy: set[str] = set()  # 只读层中尚未迁移的旧格式技能
        self.reload()

    # ── 加载 ──

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
            self._origin.clear()
            self._legacy.clear()
            self.untrusted.clear()
            for layer in [*self.readonly_dirs, self.root]:  # 后加载的覆盖先加载的
                if not layer.is_dir():
                    logger.warning("技能目录不存在，跳过: %s", layer)
                    continue
                for d in sorted(p for p in layer.iterdir() if p.is_dir() and not p.name.startswith(".")):
                    try:
                        skill = load_skill_dir(d)
                    except Exception:  # noqa: BLE001
                        logger.exception("技能目录无法加载，跳过: %s", d)
                        continue
                    if skill is None:
                        continue
                    if (d / LEGACY_MANIFEST).is_file() and not (d / SKILL_FILE).is_file():
                        if layer == self.root:
                            d = self._migrate_legacy(d, skill)
                        else:
                            self._legacy.add(skill.id)
                    skill.stats = self._load_stats(skill.id)
                    self.trust.pop(skill.id, None)
                    self.untrusted.discard(skill.id)
                    if layer != self.root and self.verifier and layer.resolve() not in self.trusted_dirs:
                        ok, note = self.verifier(d)
                        self.trust[skill.id] = note
                        if not ok:
                            self.untrusted.add(skill.id)
                            if skill.status == "active":
                                self._quarantine(skill, note, run_shadow=self.untrusted_mode == "shadow")
                    self._cache[skill.id] = skill
                    self._origin[skill.id] = d

    def _migrate_legacy(self, d: Path, skill: Skill) -> Path:
        """把可写层里的旧格式技能就地改写成 SKILL.md 格式"""
        logger.info("迁移旧格式技能 %s → SKILL.md", skill.id)
        target = d.parent / skill.id
        if target != d:
            if target.exists():
                raise FileExistsError(f"迁移目标已存在: {target}")
            old_stats = self.stats_dir / f"{d.name}.json"
            d.rename(target)
            if old_stats.exists():
                old_stats.rename(self.stats_dir / f"{skill.id}.json")
            d = target
        for v in skill.versions:
            old = d / f"v{v.version}.py"
            if old.exists():
                (d / "scripts").mkdir(exist_ok=True)
                old.replace(d / script_relpath(v.version))
        self._write_skill(d, skill)
        (d / LEGACY_MANIFEST).unlink()
        return d

    @staticmethod
    def _quarantine(skill: Skill, note: str, *, run_shadow: bool) -> None:
        """不可信的共享技能：仅在内存中改为影子状态（共享库本身不会被修改）"""
        skill.status = "shadow"
        if run_shadow and skill.kind == "flow":
            logger.warning("共享技能 %s 不可信（%s），进入影子运行（会在本进程中执行其脚本）", skill.id, note)
            if skill.shadow is None:
                skill.shadow = ShadowState(version=skill.active_version)
        else:
            logger.info("共享技能 %s 不可信（%s），已隔离：不会执行，签名或 install 后才可使用", skill.id, note)
            skill.shadow = None

    def _load_stats(self, skill_id: str) -> SkillStats:
        p = self.stats_dir / f"{skill_id}.json"
        if p.exists():
            try:
                return SkillStats.model_validate_json(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("技能统计损坏，重置: %s", p)
        return SkillStats()

    # ── 查询 ──

    def list(self, include_retired: bool = False) -> list[Skill]:
        return [s for s in self._cache.values() if include_retired or s.status == "active"]

    def get(self, skill_id: str) -> Skill | None:
        return self._cache.get(skill_id)

    def layer(self, skill_id: str) -> Literal["local", "shared"]:
        self._require(skill_id)
        return "local" if self._origin[skill_id] == self.root / skill_id else "shared"

    def path(self, skill_id: str) -> Path:
        self._require(skill_id)
        return self._origin[skill_id]

    def script(self, skill_id: str, version: int | None = None) -> str:
        skill = self._require(skill_id)
        v = version or skill.active_version
        if not v:
            raise KeyError(f"{skill_id} 是指南型技能，没有流程脚本")
        d = self._origin[skill_id]
        path = d / (f"v{v}.py" if skill_id in self._legacy else script_relpath(v))
        if not path.exists():
            raise KeyError(f"{skill_id} 没有版本 v{v}")
        return path.read_text(encoding="utf-8")

    # ── 写入 ──

    def create(self, skill: Skill, script: str, *, reason: str, pass_rate: float | None = None, shadow: bool = False) -> Skill:
        """新建流程型技能；shadow=True 时先进入影子运行，转正前不会被匹配使用"""
        with self._lock:
            base = slugify(skill.id or skill.name)
            sid, n = base, 2
            while sid in self._cache or (self.root / sid).exists():
                sid, n = f"{base}-{n}", n + 1
            skill.id = sid
            skill.active_version = 1
            skill.versions = [VersionInfo(version=1, reason=reason, test_pass_rate=pass_rate, status="candidate" if shadow else "active")]
            if shadow:
                skill.status = "shadow"
                skill.shadow = ShadowState(version=1)
            if not skill.guide.strip():
                skill.guide = skillmd.default_body(skill, step_docs(script), script_relpath(1))
            d = self.root / sid
            d.mkdir(parents=True)
            _write_atomic(d / script_relpath(1), script)
            self._origin[sid] = d
            self.save(skill)
            return skill

    def add_version(self, skill_id: str, script: str, *, reason: str, pass_rate: float | None, activate: bool = True) -> Skill:
        """
        新增版本。activate=True 直接上线；False 作为候选版本进入影子运行（替换掉之前的候选）。
        """
        with self._lock:
            skill = self._require(skill_id)
            d = self._ensure_local(skill_id)
            v = max((x.version for x in skill.versions), default=0) + 1
            _write_atomic(d / script_relpath(v), script)
            parent = skill.shadow_version or skill.active_version or None
            skill.versions.append(VersionInfo(version=v, reason=reason, parent=parent, test_pass_rate=pass_rate, status="candidate"))
            if skill.shadow:  # 被新的候选取代
                self._version(skill, skill.shadow.version).status = "rejected"
                self._version(skill, skill.shadow.version).note = f"被 v{v} 取代"
            if activate:
                self._activate(skill, v)
            else:
                skill.shadow = ShadowState(version=v)
                if skill.status == "shadow":  # 新技能还在影子期：直接以新版本继续
                    skill.active_version = v
            self.save(skill)
            return skill

    def promote(self, skill_id: str) -> Skill:
        """候选版本转正"""
        with self._lock:
            skill = self._require(skill_id)
            if skill.shadow is None:
                raise ValueError(f"{skill_id} 没有候选版本")
            self._activate(skill, skill.shadow.version)
            skill.status = "active"
            self.save(skill)
            return skill

    def reject_shadow(self, skill_id: str, reason: str) -> Skill:
        """淘汰候选版本；新技能的唯一版本被淘汰时整个技能下线"""
        with self._lock:
            skill = self._require(skill_id)
            if skill.shadow is None:
                raise ValueError(f"{skill_id} 没有候选版本")
            info = self._version(skill, skill.shadow.version)
            info.status, info.note = "rejected", reason
            skill.shadow = None
            if skill.status == "shadow":
                skill.status = "retired"
            self.save(skill)
            return skill

    def save_shadow(self, skill: Skill) -> None:
        """保存影子运行进度"""
        self.save(skill)

    def _activate(self, skill: Skill, version: int) -> None:
        old = skill.active_version
        self.trust.pop(skill.id, None)  # 转正后成为本地技能
        self.untrusted.discard(skill.id)
        if old and old != version and skill.guide:  # 正文里引用的脚本路径跟着更新
            skill.guide = skill.guide.replace(script_relpath(old), script_relpath(version))
        elif not old and skill.guide and script_relpath(version) not in skill.guide:  # 指南型升级为流程型
            skill.guide = skill.guide.rstrip() + (
                f"\n\n## 自动执行\n\n本技能已由 No-Agent 从实际使用中提炼出流程脚本 `{script_relpath(version)}`，"
                "可直接执行；失败时由 AI 接管并自动修补。\n"
            )
        for x in skill.versions:
            if x.status == "active" and x.version != version:
                x.status = "superseded"
        self._version(skill, version).status = "active"
        skill.active_version = version
        if skill.shadow and skill.shadow.version == version:
            skill.shadow = None
        skill.stats.recent = []  # 新版本重新积累成功率

    @staticmethod
    def _version(skill: Skill, version: int) -> VersionInfo:
        for x in skill.versions:
            if x.version == version:
                return x
        raise KeyError(f"{skill.id} 没有版本 v{version}")

    def set_active(self, skill_id: str, version: int) -> Skill:
        with self._lock:
            skill = self._require(skill_id)
            self._version(skill, version)
            self._activate(skill, version)
            skill.status = "active"
            self.save(skill)
            return skill

    def set_status(self, skill_id: str, status: str) -> Skill:
        with self._lock:
            skill = self._require(skill_id)
            skill.status = status  # type: ignore[assignment]
            self.save(skill)
            return skill

    def add_test(self, skill: Skill, case: TestCase, max_cases: int) -> None:
        with self._lock:
            skill.tests = [t for t in skill.tests if t.params != case.params] + [case]
            skill.tests = skill.tests[-max_cases:]
            self.save(skill)

    def install(self, src: Path, *, overwrite: bool = False) -> Skill:
        """
        从任意目录安装一个技能到可写层（复制）。src 可以是本框架的技能，也可以是普通的 Anthropic Skill。
        安装前校验 SKILL.md 和所有流程脚本。
        """
        src = Path(src)
        skill = load_skill_dir(src)
        if skill is None:
            raise ValueError(f"{src} 不是技能目录（缺少 {SKILL_FILE}）")
        if (src / SKILL_FILE).is_file():
            meta, _ = skillmd.parse((src / SKILL_FILE).read_text(encoding="utf-8"))
            if errors := skillmd.check_meta(meta):
                raise ValueError("SKILL.md 不合法：" + "；".join(errors))
        for v in skill.versions:
            p = src / script_relpath(v.version)
            if not p.is_file():
                p = src / f"v{v.version}.py"  # 旧格式
            try:
                source = p.read_text(encoding="utf-8")
                validate_script(source)
            except (OSError, ScriptError) as e:
                raise ValueError(f"流程脚本 v{v.version} 不合法: {e}") from e
            declared = set(skill.permissions.tools)
            if declared and (undeclared := script_tools(source) - declared - ALWAYS_ALLOWED):
                raise ValueError(f"流程脚本 v{v.version} 使用了未声明的工具: {', '.join(sorted(undeclared))}")
        with self._lock:
            target = self.root / skill.id
            if target.exists():
                if not overwrite:
                    raise FileExistsError(f"本地已有技能 {skill.id}（可传 overwrite=True 覆盖）")
                shutil.rmtree(target)
            shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            self.reload()
            return self._require(skill.id)

    def delete(self, skill_id: str) -> None:
        with self._lock:
            self._require(skill_id)
            if self.layer(skill_id) == "shared":
                raise PermissionError(f"{skill_id} 属于只读技能库，不能删除（可以 retire 下线）")
            shutil.rmtree(self.root / skill_id)
            (self.stats_dir / f"{skill_id}.json").unlink(missing_ok=True)
            self.reload()  # 若只读层有同名技能，会重新露出来

    def save(self, skill: Skill) -> None:
        """保存技能定义（只读层的技能会先复制到可写层）"""
        with self._lock:
            d = self._ensure_local(skill.id)
            skill.updated_at = datetime.now()
            skill.schema_version = SCHEMA_VERSION
            self._write_skill(d, skill)
            self._cache[skill.id] = skill
            self.save_stats(skill)

    def _write_skill(self, d: Path, skill: Skill) -> None:
        meta: dict[str, Any] = {}
        if (d / SKILL_FILE).is_file():
            try:
                meta, _ = skillmd.parse((d / SKILL_FILE).read_text(encoding="utf-8"))
            except skillmd.SkillFormatError:
                meta = {}
        meta = {"name": skill.id, "description": skill.description[: skillmd.DESCRIPTION_MAX], **{k: v for k, v in meta.items() if k not in ("name", "description")}}
        body = skill.guide.strip() or skillmd.default_body(skill, [], None)
        _write_atomic(d / SKILL_FILE, skillmd.render(meta, body))
        ext = skill.model_dump(mode="json", exclude=_EXT_EXCLUDE)
        _write_atomic(d / EXT_FILE, json.dumps(ext, ensure_ascii=False, indent=2))
        if skill.tests or (d / TESTS_FILE).exists():
            _write_atomic(d / TESTS_FILE, json.dumps([t.model_dump(mode="json") for t in skill.tests], ensure_ascii=False, indent=1))

    def save_stats(self, skill: Skill) -> None:
        """只保存运行统计（不触发写时复制）"""
        with self._lock:
            _write_atomic(self.stats_dir / f"{skill.id}.json", skill.stats.model_dump_json(indent=2))
            self._cache[skill.id] = skill

    def _ensure_local(self, skill_id: str) -> Path:
        local = self.root / skill_id
        origin = self._origin.get(skill_id)
        if origin is not None and origin != local:
            logger.info("技能 %s 来自只读库 %s，复制到本地后修改", skill_id, origin)
            shutil.copytree(origin, local, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if skill_id in self._legacy:  # 只读层的旧格式：复制后在本地迁移
                skill = self._cache[skill_id]
                for v in skill.versions:
                    old = local / f"v{v.version}.py"
                    if old.exists():
                        (local / "scripts").mkdir(exist_ok=True)
                        old.replace(local / script_relpath(v.version))
                (local / LEGACY_MANIFEST).unlink(missing_ok=True)
                self._legacy.discard(skill_id)
        local.mkdir(parents=True, exist_ok=True)
        self._origin[skill_id] = local
        return local

    def _require(self, skill_id: str) -> Skill:
        skill = self._cache.get(skill_id)
        if skill is None:
            raise KeyError(f"技能不存在: {skill_id}")
        return skill
