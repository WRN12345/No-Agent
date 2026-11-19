"""
文件化技能库（分层）

  只读层（extra_skill_dirs）：团队共享 / 随包分发的技能库，框架不会写入
  可写层（skills_dir）：      本项目学到的技能，以及从只读层"写时复制"过来并修改过的技能

同 id 时可写层覆盖只读层。只读层的技能被修补 / 切换版本 / 下线时，先整体复制到可写层再修改，
之后本地版本优先（上游再更新也不会覆盖本地修改）。

每个技能一个目录：
  <id>/manifest.json   技能定义、版本历史、测试用例
  <id>/v1.py, v2.py    各版本脚本
运行统计（高频变化的运行时数据）单独存在 <home>/.stats/<id>.json，不进 git，也不会触发写时复制。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from no_agent.skills.models import Skill, SkillStats, TestCase, VersionInfo
from no_agent.storage.base import SCHEMA_VERSION

logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return s[:48] or "skill"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class FileSkillStore:
    def __init__(self, root: Path, *, readonly_dirs: list[Path] | None = None, stats_dir: Path | None = None) -> None:
        self.root = Path(root)
        self.readonly_dirs = [Path(d) for d in (readonly_dirs or [])]
        self.stats_dir = Path(stats_dir) if stats_dir else self.root.parent / ".stats"
        self.root.mkdir(parents=True, exist_ok=True)
        self.stats_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, Skill] = {}
        self._origin: dict[str, Path] = {}  # 技能所在目录
        self.reload()

    # ── 加载 ──

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
            self._origin.clear()
            for layer in [*self.readonly_dirs, self.root]:  # 后加载的覆盖先加载的
                if not layer.is_dir():
                    logger.warning("技能目录不存在，跳过: %s", layer)
                    continue
                for manifest in layer.glob("*/manifest.json"):
                    try:
                        skill = Skill.model_validate_json(manifest.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        logger.exception("技能清单损坏，跳过: %s", manifest)
                        continue
                    if skill.schema_version > SCHEMA_VERSION:
                        logger.warning("技能 %s 的结构版本 %s 高于当前框架支持的 %s，跳过", skill.id, skill.schema_version, SCHEMA_VERSION)
                        continue
                    skill.stats = self._load_stats(skill.id)
                    self._cache[skill.id] = skill
                    self._origin[skill.id] = manifest.parent

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

    def script(self, skill_id: str, version: int | None = None) -> str:
        skill = self._require(skill_id)
        v = version or skill.active_version
        path = self._origin[skill_id] / f"v{v}.py"
        if not path.exists():
            raise KeyError(f"{skill_id} 没有版本 v{v}")
        return path.read_text(encoding="utf-8")

    # ── 写入 ──

    def create(self, skill: Skill, script: str, *, reason: str, pass_rate: float | None = None) -> Skill:
        with self._lock:
            base = slugify(skill.id or skill.name)
            sid, n = base, 2
            while sid in self._cache or (self.root / sid).exists():
                sid, n = f"{base}_{n}", n + 1
            skill.id = sid
            skill.active_version = 1
            skill.versions = [VersionInfo(version=1, reason=reason, test_pass_rate=pass_rate)]
            d = self.root / sid
            d.mkdir(parents=True)
            (d / "v1.py").write_text(script, encoding="utf-8")
            self._origin[sid] = d
            self.save(skill)
            return skill

    def add_version(self, skill_id: str, script: str, *, reason: str, pass_rate: float | None) -> Skill:
        with self._lock:
            skill = self._require(skill_id)
            d = self._ensure_local(skill_id)
            v = max(x.version for x in skill.versions) + 1
            (d / f"v{v}.py").write_text(script, encoding="utf-8")
            skill.versions.append(VersionInfo(version=v, reason=reason, parent=skill.active_version, test_pass_rate=pass_rate))
            skill.active_version = v
            skill.stats.recent = []  # 新版本重新积累成功率
            self.save(skill)
            return skill

    def set_active(self, skill_id: str, version: int) -> Skill:
        with self._lock:
            skill = self._require(skill_id)
            if not any(x.version == version for x in skill.versions):
                raise KeyError(f"{skill_id} 没有版本 v{version}")
            skill.active_version = version
            skill.status = "active"
            skill.stats.recent = []
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
            data = skill.model_dump(mode="json", exclude={"stats"})
            _write_atomic(d / "manifest.json", json.dumps(data, ensure_ascii=False, indent=2))
            self._cache[skill.id] = skill
            self.save_stats(skill)

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
            shutil.copytree(origin, local, dirs_exist_ok=True)
        local.mkdir(parents=True, exist_ok=True)
        self._origin[skill_id] = local
        return local

    def _require(self, skill_id: str) -> Skill:
        skill = self._cache.get(skill_id)
        if skill is None:
            raise KeyError(f"技能不存在: {skill_id}")
        return skill
