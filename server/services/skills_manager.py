"""Skills manager for copying and managing Turbine Lens skills.

Adapted from databricks-builder-app server/services/skills_manager.py.
Handles discovering skills from source directories and syncing them into
the agent working directory so Claude Code can load them via
setting_sources=['project'].
"""

import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill -> MCP tool mapping
# Maps skill names to their exclusive Databricks MCP tool function names.
# Tools NOT listed here (execute_sql, get_table_schema, genie_*) are always
# available regardless of which skills are enabled.
#
# Turbine Lens current skills are pure guidance skills (no MCP-tool gating).
# Extend this dict if you add skills that should gate specific MCP tools.
# ---------------------------------------------------------------------------
SKILL_TOOL_MAPPING: dict[str, list[str]] = {
    # Example:
    # 'databricks-vector-search': ['manage_vs_endpoint', 'manage_vs_index'],
}


def get_allowed_mcp_tools(
    all_tool_names: list[str],
    enabled_skills: list[str] | None = None,
) -> list[str]:
    """Filter MCP tool names based on enabled skills.

    Tools mapped to disabled skills are removed. Tools not mapped to any
    skill (e.g. execute_sql, genie tools) are always kept.
    """
    if enabled_skills is None:
        return all_tool_names

    enabled_set = set(enabled_skills)
    blocked_tools: set[str] = set()
    for skill_name, tool_names in SKILL_TOOL_MAPPING.items():
        if skill_name not in enabled_set:
            blocked_tools.update(tool_names)

    for skill_name in enabled_skills:
        for tool_name in SKILL_TOOL_MAPPING.get(skill_name, []):
            blocked_tools.discard(tool_name)

    prefix = "mcp__databricks__"
    return [t for t in all_tool_names if not t.startswith(prefix) or t[len(prefix) :] not in blocked_tools]


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
# _APP_ROOT is the `application/` directory (one level above src/).
_APP_ROOT = Path(__file__).parent.parent.parent  # services → server → project root

# Where skills live inside the deployed app bundle.
_DEPLOYED_SKILLS_DIR = _APP_ROOT / "skills"

# Populated by an install script that merges multiple skill repos.
_INSTALLED_SKILLS_DIR = _APP_ROOT / ".claude" / "skills"

# Optional sibling repo for local development.
_DEV_SKILLS_DIR = _APP_ROOT.parent / "databricks-skills"

# Canonical local skills cache used by the app at runtime.
APP_SKILLS_DIR = _APP_ROOT / "skills"
APP_AGENTS_DIR = _APP_ROOT / ".claude" / "agents"


def _non_empty_dir(p: Path) -> bool:
    return p.exists() and p.is_dir() and any(p.iterdir())


# Build an ordered list of source directories. First dir that contains a
# given skill wins, so put the most-complete source first.
_SKILLS_SOURCE_DIRS: list[Path] = []
if _non_empty_dir(_INSTALLED_SKILLS_DIR):
    _SKILLS_SOURCE_DIRS.append(_INSTALLED_SKILLS_DIR)
if _non_empty_dir(_DEV_SKILLS_DIR):
    _SKILLS_SOURCE_DIRS.append(_DEV_SKILLS_DIR)
if _non_empty_dir(_DEPLOYED_SKILLS_DIR) and _DEPLOYED_SKILLS_DIR.resolve() != APP_SKILLS_DIR.resolve():
    _SKILLS_SOURCE_DIRS.append(_DEPLOYED_SKILLS_DIR)

# Legacy single-directory reference for callers not yet updated.
SKILLS_SOURCE_DIR = _SKILLS_SOURCE_DIRS[0] if _SKILLS_SOURCE_DIRS else _DEV_SKILLS_DIR


def _get_enabled_skills() -> list[str] | None:
    """Get list of enabled skills from the ENABLED_SKILLS env var.

    Returns None to include all skills, or a list of skill names.
    """
    enabled = os.environ.get("ENABLED_SKILLS", "").strip()
    if not enabled:
        return None
    return [s.strip() for s in enabled.split(",") if s.strip()]


def _parse_skill_name(skill_dir: Path) -> str | None:
    """Parse the skill name from SKILL.md / SKILL.MD frontmatter."""
    for filename in ("SKILL.md", "SKILL.MD"):
        skill_md = skill_dir / filename
        if skill_md.exists():
            try:
                content = skill_md.read_text()
                if content.startswith("---"):
                    end_idx = content.find("---", 3)
                    if end_idx > 0:
                        for line in content[3:end_idx].strip().split("\n"):
                            if line.startswith("name:"):
                                return line.split(":", 1)[1].strip().strip("\"'")
            except Exception:
                pass
    return None


def _find_skill_source(skill_name: str) -> Path | None:
    """Return the first source dir containing skill_name/SKILL.md."""
    for src_dir in _SKILLS_SOURCE_DIRS:
        candidate = src_dir / skill_name
        if candidate.is_dir() and any((candidate / f).exists() for f in ("SKILL.md", "SKILL.MD")):
            return candidate
    return None


class SkillNotFoundError(Exception):
    """Raised when an enabled skill is not found in any source directory."""


def copy_skills_to_app() -> bool:
    """Copy skills from source directories into APP_SKILLS_DIR.

    Called on server startup to ensure the latest skills are in place.
    When APP_SKILLS_DIR IS the source (deployed bundle), this is a no-op.

    Raises SkillNotFoundError if an ENABLED_SKILLS entry is missing.
    """
    if not _SKILLS_SOURCE_DIRS:
        if _non_empty_dir(APP_SKILLS_DIR):
            logger.info("No external source dirs; skills already in place at %s", APP_SKILLS_DIR)
            return True
        logger.warning("No skills source directories found")
        return False

    all_same = all(src.resolve() == APP_SKILLS_DIR.resolve() for src in _SKILLS_SOURCE_DIRS)
    if all_same:
        logger.info("All skills sources resolve to %s, skipping copy", APP_SKILLS_DIR)
        return True

    enabled_skills = _get_enabled_skills()
    if enabled_skills:
        logger.info("Filtering skills to: %s", enabled_skills)
        for skill_name in enabled_skills:
            if _find_skill_source(skill_name) is None:
                searched = ", ".join(str(d) for d in _SKILLS_SOURCE_DIRS)
                raise SkillNotFoundError(
                    f"Skill '{skill_name}' not found in any source directory. "
                    f"Searched: {searched}. Check ENABLED_SKILLS."
                )

    try:
        if APP_SKILLS_DIR.exists():
            shutil.rmtree(APP_SKILLS_DIR)
        APP_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        copied: set[str] = set()
        for src_dir in _SKILLS_SOURCE_DIRS:
            if src_dir.resolve() == APP_SKILLS_DIR.resolve():
                continue
            for item in src_dir.iterdir():
                if not item.is_dir():
                    continue
                has_md = any((item / f).exists() for f in ("SKILL.md", "SKILL.MD"))
                if not has_md or item.name in copied:
                    continue
                if enabled_skills and item.name not in enabled_skills:
                    logger.debug("Skipping skill (not enabled): %s", item.name)
                    continue
                shutil.copytree(item, APP_SKILLS_DIR / item.name)
                copied.add(item.name)
                logger.debug("Copied skill: %s", item.name)

        logger.info("Copied %d skills to %s", len(copied), APP_SKILLS_DIR)
        return True
    except SkillNotFoundError:
        raise
    except Exception as e:
        logger.error("Failed to copy skills: %s", e)
        return False


def get_available_skills(enabled_skills: list[str] | None = None) -> list[dict]:
    """Return available skills with name, description, and path."""
    skills = []
    if not APP_SKILLS_DIR.exists():
        logger.warning("Skills directory not found: %s", APP_SKILLS_DIR)
        return skills

    for skill_dir in APP_SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = next(
            (skill_dir / f for f in ("SKILL.md", "SKILL.MD") if (skill_dir / f).exists()),
            None,
        )
        if skill_md is None:
            continue
        try:
            content = skill_md.read_text()
            if content.startswith("---"):
                end_idx = content.find("---", 3)
                if end_idx > 0:
                    name = description = None
                    for line in content[3:end_idx].strip().split("\n"):
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip("\"'")
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip().strip("\"'")
                    if name:
                        if enabled_skills is not None and name not in enabled_skills:
                            continue
                        skills.append({"name": name, "description": description or "", "path": str(skill_dir)})
        except Exception as e:
            logger.warning("Failed to parse skill %s: %s", skill_dir, e)

    return skills


def copy_skills_and_agents_to_project(
    project_dir: Path,
    enabled_skills: list[str] | None = None,
) -> bool:
    """Copy skills from APP_SKILLS_DIR into project_dir/.claude/skills/.

    Args:
        project_dir: Path to the agent working directory.
        enabled_skills: Optional list of skill names to copy (None = all).
    """
    if not APP_SKILLS_DIR.exists():
        logger.warning("App skills directory not found, attempting refresh")
        copy_skills_to_app()
    if not APP_SKILLS_DIR.exists():
        logger.warning("No skills available to copy")
        return False

    enabled_dir_names = None
    if enabled_skills is not None:
        enabled_dir_names = set()
        for skill_dir in APP_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = _parse_skill_name(skill_dir)
            if skill_name and skill_name in enabled_skills:
                enabled_dir_names.add(skill_dir.name)

    try:
        project_skills_dir = project_dir / ".claude" / "skills"
        project_skills_dir.mkdir(parents=True, exist_ok=True)

        count = 0
        for skill_dir in APP_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            if not any((skill_dir / f).exists() for f in ("SKILL.md", "SKILL.MD")):
                continue
            if enabled_dir_names is not None and skill_dir.name not in enabled_dir_names:
                continue
            dest = project_skills_dir / skill_dir.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
            count += 1

        logger.info("Copied %d skills to project: %s", count, project_dir)
        return True
    except Exception as e:
        logger.error("Failed to copy skills to project: %s", e)
        return False


def sync_project_skills(
    project_dir: Path,
    enabled_skills: list[str] | None = None,
) -> bool:
    """Sync project skills incrementally (remove disabled, add missing)."""
    if not APP_SKILLS_DIR.exists():
        logger.warning("App skills directory not found")
        return False
    try:
        project_skills_dir = project_dir / ".claude" / "skills"
        project_skills_dir.mkdir(parents=True, exist_ok=True)

        name_to_dir: dict[str, str] = {}
        for skill_dir in APP_SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = _parse_skill_name(skill_dir)
            if skill_name:
                name_to_dir[skill_name] = skill_dir.name

        desired_dirs = (
            {name_to_dir[n] for n in enabled_skills if n in name_to_dir}
            if enabled_skills is not None
            else set(name_to_dir.values())
        )

        for existing in project_skills_dir.iterdir():
            if existing.is_dir() and existing.name not in desired_dirs:
                logger.debug("Removing disabled skill: %s", existing.name)
                shutil.rmtree(existing)

        for dir_name in desired_dirs:
            dest = project_skills_dir / dir_name
            if not dest.exists():
                src = APP_SKILLS_DIR / dir_name
                if src.exists():
                    logger.debug("Adding skill: %s", dir_name)
                    shutil.copytree(src, dest)

        logger.info("Synced project skills: %d enabled", len(desired_dirs))
        return True
    except Exception as e:
        logger.error("Failed to sync project skills: %s", e)
        return False


def sync_project_agents(project_dir: Path) -> bool:
    """Provision app-level agent definitions into project_dir/.claude/agents/.

    Only provisions items that do not yet exist. Delete the item from
    project_dir/.claude/agents/ to force re-provisioning.
    """
    if not APP_AGENTS_DIR.exists():
        logger.debug("No agents directory at app root, skipping")
        return True
    try:
        dest_dir = project_dir / ".claude" / "agents"
        dest_dir.mkdir(parents=True, exist_ok=True)

        provisioned = 0
        for item in APP_AGENTS_DIR.iterdir():
            dest_item = dest_dir / item.name
            if dest_item.exists():
                continue
            if item.is_file():
                dest_item.write_bytes(item.read_bytes())
            elif item.is_dir():
                shutil.copytree(item, dest_item)
            provisioned += 1

        if provisioned:
            logger.info("Provisioned %d agent item(s) to project: %s", provisioned, project_dir)
        return True
    except Exception as e:
        logger.error("Failed to provision agent definitions: %s", e)
        return False


def reload_project_skills(
    project_dir: Path,
    enabled_skills: list[str] | None = None,
) -> bool:
    """Refresh from source, wipe project skills, and recopy."""
    try:
        logger.info("Refreshing app skills from source...")
        copy_skills_to_app()
        project_skills_dir = project_dir / ".claude" / "skills"
        if project_skills_dir.exists():
            shutil.rmtree(project_skills_dir)
        return copy_skills_and_agents_to_project(project_dir, enabled_skills=enabled_skills)
    except Exception as e:
        logger.error("Failed to reload project skills: %s", e)
        return False


def get_skills_summary() -> str:
    """Return a Markdown summary of available skills for the system prompt."""
    skills = get_available_skills()
    if not skills:
        return ""
    lines = [
        "## Available Skills",
        "",
        "Use the `Skill` tool to invoke these skills for specialised guidance:",
        "",
    ]
    for skill in skills:
        lines.append(f"- **{skill['name']}**: {skill['description']}")
    lines += ["", 'To use a skill, invoke it by name (e.g., `skill: "turbine_lens-graph"`).']
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File-based enabled skills storage — project_dir/.claude/enabled_skills.json
# ---------------------------------------------------------------------------
_ENABLED_SKILLS_FILENAME = "enabled_skills.json"


def get_project_enabled_skills(project_dir: Path) -> list[str] | None:
    """Read the enabled skills list from the filesystem (None = all enabled)."""
    config_path = project_dir / ".claude" / _ENABLED_SKILLS_FILENAME
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text())
        return data if isinstance(data, list) else None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read enabled skills config: %s", e)
        return None


def set_project_enabled_skills(
    project_dir: Path,
    enabled_skills: list[str] | None,
) -> bool:
    """Write the enabled skills list to the filesystem (None = all enabled)."""
    claude_dir = project_dir / ".claude"
    config_path = claude_dir / _ENABLED_SKILLS_FILENAME
    try:
        if enabled_skills is None:
            if config_path.exists():
                config_path.unlink()
        else:
            claude_dir.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(enabled_skills, indent=2))
        return True
    except OSError as e:
        logger.error("Failed to write enabled skills config: %s", e)
        return False
