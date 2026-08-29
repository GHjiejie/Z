from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from packages.persistence import Database


class SkillRegistry:
    """Read-side registry and immutable reference resolver for published plans."""

    def __init__(self, db: Database):
        self.db = db

    def list_plugins(self) -> List[Dict[str, Any]]:
        plugins = self.db.fetch_all("SELECT * FROM plugins WHERE status='ACTIVE' ORDER BY name")
        for plugin in plugins:
            plugin.pop("source_path", None)
            plugin["skill_count"] = self.db.fetch_one(
                "SELECT COUNT(*) AS count FROM skills WHERE plugin_id=? AND status='ACTIVE'",
                (plugin["id"],),
            )["count"]
        return plugins

    def list_skills(self) -> List[Dict[str, Any]]:
        return self.db.fetch_all(
            """SELECT s.*, p.name AS plugin_name, v.version, v.artifact_hash
               FROM skills s
               JOIN plugins p ON p.id=s.plugin_id
               JOIN skill_versions v ON v.id=s.current_version_id
               WHERE s.status='ACTIVE' AND p.status='ACTIVE'
               ORDER BY s.name"""
        )

    def get_skill(self, reference: str, include_content: bool = True) -> Optional[Dict[str, Any]]:
        version = self._resolve(reference)
        if version:
            version.pop("source_path", None)
            if not include_content:
                version.pop("content", None)
        return version

    def resolve_many(self, references: Iterable[str]) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        for reference in references:
            skill = self._resolve(reference)
            if not skill:
                raise LookupError(f"Skill reference does not exist or is inactive: {reference}")
            resolved.append(
                {
                    "revision_id": skill["id"],
                    "skill_id": skill["skill_id"],
                    "slug": skill["slug"],
                    "name": skill["name"],
                    "description": skill["description"],
                    "version": skill["version"],
                    "artifact_hash": skill["artifact_hash"],
                    "plugin_id": skill["plugin_id"],
                    "instructions": skill["content"],
                }
            )
        return resolved

    def _resolve(self, reference: str) -> Optional[Dict[str, Any]]:
        normalized = reference.removeprefix("builtin:")
        if normalized.startswith("skillv_"):
            return self._version_query("v.id=?", (normalized,))
        if "@" in normalized:
            slug, version = normalized.rsplit("@", 1)
            return self._version_query("s.slug=? AND v.version=?", (slug, version))
        return self._version_query(
            "(s.slug=? OR s.id=?) AND v.id=s.current_version_id", (normalized, normalized)
        )

    def _version_query(self, predicate: str, params: tuple[Any, ...]) -> Optional[Dict[str, Any]]:
        return self.db.fetch_one(
            f"""SELECT v.*, s.slug, s.name, s.description, s.plugin_id, s.tags_json,
                       p.name AS plugin_name
                FROM skill_versions v
                JOIN skills s ON s.id=v.skill_id
                JOIN plugins p ON p.id=s.plugin_id
                WHERE {predicate} AND s.status='ACTIVE' AND p.status='ACTIVE'""",
            params,
        )
