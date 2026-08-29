from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from packages.domain.models import utc_now
from packages.persistence import Database


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?$")


class PluginLoadError(RuntimeError):
    """Raised when a plugin package is unsafe, malformed, or mutates a locked skill."""


@dataclass(frozen=True)
class PluginLoadReport:
    plugin_count: int
    skill_count: int
    plugin_ids: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "plugin_count": self.plugin_count,
            "skill_count": self.skill_count,
            "plugin_ids": self.plugin_ids,
        }


@dataclass(frozen=True)
class DiscoveredSkill:
    id: str
    slug: str
    name: str
    version: str
    description: str
    tags: List[str]
    content: str
    artifact_hash: str
    source_path: str


@dataclass(frozen=True)
class DiscoveredPlugin:
    id: str
    name: str
    version: str
    description: str
    source_path: str
    manifest_hash: str
    skills: List[DiscoveredSkill]


class PluginLoader:
    """Discovers declarative plugin bundles and registers immutable Skill versions.

    Phase 1 plugins are data-only packages. They may contribute reviewed instruction
    artifacts, but startup never imports or executes Python from a plugin directory.
    """

    MANIFEST_NAME = "plugin.json"

    def __init__(self, db: Database, roots: Iterable[Path]):
        self.db = db
        self.roots = [Path(root).resolve() for root in roots]

    def load(self) -> PluginLoadReport:
        plugins = self._discover_all()
        # Registry visibility mirrors the packages present during this startup.
        # Historical versions remain stored so old execution plans stay auditable.
        self.db.execute("UPDATE plugins SET status='INACTIVE'")
        self.db.execute("UPDATE skills SET status='INACTIVE'")
        for plugin in plugins:
            self._register(plugin)
        return PluginLoadReport(
            plugin_count=len(plugins),
            skill_count=sum(len(plugin.skills) for plugin in plugins),
            plugin_ids=[plugin.id for plugin in plugins],
        )

    def _discover_all(self) -> List[DiscoveredPlugin]:
        manifests: List[Path] = []
        for root in self.roots:
            if not root.exists():
                continue
            if not root.is_dir():
                raise PluginLoadError(f"Plugin root is not a directory: {root}")
            manifests.extend(sorted(root.glob(f"*/{self.MANIFEST_NAME}")))

        plugins = [self._read_manifest(path) for path in manifests]
        plugin_ids: set[str] = set()
        skill_slugs: set[str] = set()
        for plugin in plugins:
            if plugin.id in plugin_ids:
                raise PluginLoadError(f"Duplicate plugin id: {plugin.id}")
            plugin_ids.add(plugin.id)
            for skill in plugin.skills:
                if skill.slug in skill_slugs:
                    raise PluginLoadError(f"Duplicate skill slug across loaded plugins: {skill.slug}")
                skill_slugs.add(skill.slug)
        return plugins

    def _read_manifest(self, path: Path) -> DiscoveredPlugin:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginLoadError(f"Unable to read plugin manifest {path}: {exc}") from exc
        if manifest.get("schema_version") != "1.0":
            raise PluginLoadError(f"{path}: schema_version must be 1.0")

        plugin_id = self._identifier(manifest.get("id"), "plugin id", path)
        version = self._version(manifest.get("version"), "plugin version", path)
        name = self._required_text(manifest.get("name"), "plugin name", path)
        description = self._required_text(manifest.get("description"), "plugin description", path)
        raw_skills = manifest.get("skills")
        if not isinstance(raw_skills, list) or not raw_skills:
            raise PluginLoadError(f"{path}: skills must be a non-empty list")

        plugin_root = path.parent.resolve()
        skills = [self._read_skill(plugin_id, plugin_root, item, path) for item in raw_skills]
        canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        hash_material = canonical + "".join(skill.artifact_hash for skill in skills)
        return DiscoveredPlugin(
            id=plugin_id,
            name=name,
            version=version,
            description=description,
            source_path=str(plugin_root),
            manifest_hash=hashlib.sha256(hash_material.encode("utf-8")).hexdigest(),
            skills=skills,
        )

    def _read_skill(
        self,
        plugin_id: str,
        plugin_root: Path,
        raw: Any,
        manifest_path: Path,
    ) -> DiscoveredSkill:
        if not isinstance(raw, dict):
            raise PluginLoadError(f"{manifest_path}: each skill must be an object")
        slug = self._identifier(raw.get("slug"), "skill slug", manifest_path)
        version = self._version(raw.get("version"), f"version for skill {slug}", manifest_path)
        name = self._required_text(raw.get("name"), f"name for skill {slug}", manifest_path)
        description = self._required_text(
            raw.get("description"), f"description for skill {slug}", manifest_path
        )
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise PluginLoadError(f"{manifest_path}: tags for {slug} must be non-empty strings")
        relative_path = raw.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            raise PluginLoadError(f"{manifest_path}: path is required for skill {slug}")
        source = (plugin_root / relative_path).resolve()
        if plugin_root not in source.parents or source.name != "SKILL.md":
            raise PluginLoadError(f"{manifest_path}: skill {slug} must reference a contained SKILL.md")
        try:
            content = source.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PluginLoadError(f"Unable to read skill {slug} from {source}: {exc}") from exc
        if not content:
            raise PluginLoadError(f"Skill {slug} is empty: {source}")
        artifact_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        version_id_material = f"{plugin_id}:{slug}:{version}"
        version_id = f"skillv_{hashlib.sha256(version_id_material.encode()).hexdigest()[:20]}"
        return DiscoveredSkill(
            id=version_id,
            slug=slug,
            name=name,
            version=version,
            description=description,
            tags=[tag.strip() for tag in tags],
            content=content,
            artifact_hash=artifact_hash,
            source_path=str(source),
        )

    def _register(self, plugin: DiscoveredPlugin) -> None:
        now = utc_now()
        existing_plugin = self.db.fetch_one("SELECT * FROM plugins WHERE id=?", (plugin.id,))
        if (
            existing_plugin
            and existing_plugin["version"] == plugin.version
            and existing_plugin["manifest_hash"] != plugin.manifest_hash
        ):
            raise PluginLoadError(
                f"Plugin {plugin.id}@{plugin.version} changed after registration; bump its version"
            )
        self.db.execute(
            """INSERT INTO plugins
               (id, name, version, description, source_path, manifest_hash, status, loaded_at)
               VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, version=excluded.version, description=excluded.description,
                 source_path=excluded.source_path, manifest_hash=excluded.manifest_hash,
                 status='ACTIVE', loaded_at=excluded.loaded_at""",
            (
                plugin.id,
                plugin.name,
                plugin.version,
                plugin.description,
                plugin.source_path,
                plugin.manifest_hash,
                now,
            ),
        )

        for skill in plugin.skills:
            skill_id = f"skill_{skill.slug.replace('.', '_').replace('-', '_')}"
            existing_owner = self.db.fetch_one("SELECT plugin_id FROM skills WHERE slug=?", (skill.slug,))
            if existing_owner and existing_owner["plugin_id"] != plugin.id:
                raise PluginLoadError(
                    f"Skill slug {skill.slug} is already owned by plugin {existing_owner['plugin_id']}"
                )
            existing_version = self.db.fetch_one(
                "SELECT artifact_hash FROM skill_versions WHERE skill_id=? AND version=?",
                (skill_id, skill.version),
            )
            if existing_version and existing_version["artifact_hash"] != skill.artifact_hash:
                raise PluginLoadError(
                    f"Skill {skill.slug}@{skill.version} changed after registration; bump its version"
                )
            self.db.execute(
                """INSERT INTO skills
                   (id, plugin_id, slug, name, description, current_version_id, tags_json,
                    status, builtin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     plugin_id=excluded.plugin_id, slug=excluded.slug, name=excluded.name,
                     description=excluded.description, current_version_id=excluded.current_version_id,
                     tags_json=excluded.tags_json, status='ACTIVE', updated_at=excluded.updated_at""",
                (
                    skill_id,
                    plugin.id,
                    skill.slug,
                    skill.name,
                    skill.description,
                    skill.id,
                    self.db.encode(skill.tags),
                    now,
                    now,
                ),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO skill_versions
                   (id, skill_id, version, artifact_hash, content, source_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    skill.id,
                    skill_id,
                    skill.version,
                    skill.artifact_hash,
                    skill.content,
                    skill.source_path,
                    now,
                ),
            )

    @staticmethod
    def _identifier(value: Any, field: str, path: Path) -> str:
        if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
            raise PluginLoadError(f"{path}: invalid {field}")
        return value

    @staticmethod
    def _version(value: Any, field: str, path: Path) -> str:
        if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
            raise PluginLoadError(f"{path}: invalid {field}; expected semantic version")
        return value

    @staticmethod
    def _required_text(value: Any, field: str, path: Path) -> str:
        if not isinstance(value, str) or not value.strip():
            raise PluginLoadError(f"{path}: {field} is required")
        return value.strip()
