#!/usr/bin/env python3
"""Scan every Claude Code skill on this machine and emit a self-contained HTML dashboard.

Features:
  - Always-loaded vs when-invoked token estimates per skill
  - Symlink-aware deduplication
  - Skill invocation counts parsed from Claude Code session logs
  - Snapshot diff: week-over-week growth tracking
  - Built-in CLI tool description costs (Bash, Read, Edit, …)
  - --fix-dupes: auto-symlink byte-identical disk duplicates

Token estimates use tiktoken (cl100k_base) if installed, else fall back to chars/4.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.3.0"

HOME = Path.home()

# tiktoken is optional — install for ~10x more accurate counts.
try:
    import tiktoken  # type: ignore
    _ENC = tiktoken.get_encoding("cl100k_base")
    TIKTOKEN_AVAILABLE = True
except Exception:
    _ENC = None
    TIKTOKEN_AVAILABLE = False


SKILL_ROOTS = [
    (HOME / ".claude" / "skills", "user", "~/.claude/skills"),
    (HOME / ".agents" / "skills", "agent-sdk", "~/.agents/skills"),
]
PLUGIN_CACHE = HOME / ".claude" / "plugins" / "cache"
SESSIONS_ROOT = HOME / ".claude" / "projects"


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        return HOME / "Library" / "Caches" / "skillchart"
    return Path(os.environ.get("XDG_CACHE_HOME", str(HOME / ".cache"))) / "skillchart"


SNAPSHOT_PATH = default_cache_dir() / "last-snapshot.json"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "skillchart" / "config.json"


# Subscription plans — approximate quotas, transparently labelled.
# Anthropic publishes ranges; these are reasonable midpoints. Override in config or via --plan.
PLANS: dict[str, dict] = {
    "free": {
        "label": "Free",
        "monthly_price_usd": 0,
        "context_window": 200_000,
        "messages_per_5h": 30,
        "code_hours_sonnet_weekly": (5, 15),
        "code_hours_opus_weekly": None,
    },
    "pro": {
        "label": "Pro ($20/mo)",
        "monthly_price_usd": 20,
        "context_window": 200_000,
        "messages_per_5h": 80,
        "code_hours_sonnet_weekly": (40, 80),
        "code_hours_opus_weekly": None,
    },
    "max-5x": {
        "label": "Max 5x ($100/mo)",
        "monthly_price_usd": 100,
        "context_window": 200_000,
        "messages_per_5h": 225,
        "code_hours_sonnet_weekly": (50, 200),
        "code_hours_opus_weekly": (5, 40),
    },
    "max-20x": {
        "label": "Max 20x ($200/mo)",
        "monthly_price_usd": 200,
        "context_window": 200_000,
        "messages_per_5h": 900,
        "code_hours_sonnet_weekly": (240, 480),
        "code_hours_opus_weekly": (24, 40),
    },
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


# Built-in skills shipped with the Claude Code CLI (no SKILL.md on disk).
BUILTIN_SKILLS = [
    ("update-config", "Use this skill to configure the Claude Code harness via settings.json. Automated behaviors (\"from now on when X\", \"each time X\", \"whenever X\", \"before/after X\") require hooks configured in settings.json - the harness executes these, not Claude, so memory/preferences cannot fulfill them. Also use for: permissions (\"allow X\", \"add permission\", \"move permission to\"), env vars (\"set X=Y\"), hook troubleshooting, or any changes to settings.json/settings.local.json files."),
    ("keybindings-help", "Use when the user wants to customize keyboard shortcuts, rebind keys, add chord bindings, or modify ~/.claude/keybindings.json."),
    ("simplify", "Review changed code for reuse, quality, and efficiency, then fix any issues found."),
    ("fewer-permission-prompts", "Scan your transcripts for common read-only Bash and MCP tool calls, then add a prioritized allowlist to project .claude/settings.json to reduce permission prompts."),
    ("loop", "Run a prompt or slash command on a recurring interval (e.g. /loop 5m /foo). Omit the interval to let the model self-pace."),
    ("schedule", "Create, update, list, or run scheduled remote agents (routines) on a cron schedule or once at a specific time. ALSO OFFER PROACTIVELY after work that has a natural future follow-up: feature flag cleanup, alert triage, removal of TODO/migration with \"remove once X\" condition. Skip the offer for refactors and bug fixes."),
    ("claude-api", "Build, debug, and optimize Claude API / Anthropic SDK apps. Apps built with this skill should include prompt caching. Also handles migrating existing Claude API code between Claude model versions."),
    ("savings", "Audit this session's token usage and flag context-eating patterns before they tank the budget."),
    ("init", "Initialize a new CLAUDE.md file with codebase documentation"),
    ("review", "Review a pull request"),
    ("security-review", "Complete a security review of the pending changes on the current branch"),
]


# Approximate Claude Code CLI tool descriptions (always-loaded in the system prompt).
# These ship in the binary, not on disk. Descriptions are abridged but representative.
BUILTIN_TOOLS = [
    ("Agent", "Launch a new agent to handle complex, multi-step tasks. Each agent type has specific capabilities and tools available to it. Available agent types: claude-code-guide, Explore, general-purpose, Plan, statusline-setup. Spawn agents for parallel research, broad codebase exploration, or open-ended questions across multiple files."),
    ("Bash", "Executes a given bash command and returns its output. The working directory persists between commands. Avoid using this tool to run cat/head/tail/sed/awk/echo unless explicitly instructed — use the dedicated Read, Edit, Write tools instead. Includes detailed git commit and PR creation protocols."),
    ("Edit", "Performs exact string replacements in files. You must use the Read tool at least once before editing. The edit will fail if old_string is not unique in the file — provide more context or use replace_all."),
    ("Read", "Reads a file from the local filesystem. Reads up to 2000 lines starting from the beginning by default. Supports images (PNG, JPG), PDFs (with pages parameter for >10 pages), and Jupyter notebooks. Always use absolute paths."),
    ("Write", "Writes a file to the local filesystem. Will overwrite existing files. If the file exists, you MUST use Read first. Prefer Edit for modifying existing files since it only sends the diff. Never create documentation files unless explicitly requested."),
    ("Skill", "Execute a skill within the main conversation. When users reference a slash command or /<something>, they are referring to a skill. Only invoke a skill that appears in the available-skills list, or one the user explicitly typed."),
    ("ToolSearch", "Fetches full schema definitions for deferred tools so they can be called. Deferred tools appear by name in system-reminder messages. Until fetched, only the name is known — there is no parameter schema, so the tool cannot be invoked."),
    ("ScheduleWakeup", "Schedule when to resume work in /loop dynamic mode. The Anthropic prompt cache has a 5-minute TTL. Sleeping past 300s means the next wake-up reads conversation context uncached. Default to 1200-1800s for idle ticks."),
]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    fm: dict = {}
    lines = fm_raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2)
        if val.strip() in (">", "|", ">-", "|-"):
            i += 1
            block_lines = []
            while i < len(lines) and (lines[i].startswith(" ") or lines[i] == ""):
                block_lines.append(lines[i].strip())
                i += 1
            fm[key] = " ".join(l for l in block_lines if l)
            continue
        fm[key] = val.strip()
        i += 1
    return fm, body


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    if TIKTOKEN_AVAILABLE:
        return len(_ENC.encode(text))
    return max(1, round(len(text) / 4))


def discover_skills() -> list[dict]:
    skills: list[dict] = []

    for root, category, label in SKILL_ROOTS:
        if not root.exists():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            skill_dir = skill_md.parent
            is_symlink = skill_dir.is_symlink()
            link_target = os.readlink(skill_dir) if is_symlink else None
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            fm, body = parse_frontmatter(text)
            name = fm.get("name") or skill_dir.name
            description = fm.get("description", "").strip()
            skills.append({
                "name": name,
                "description": description,
                "category": category,
                "source": label + "/" + skill_dir.name,
                "path": str(skill_md),
                "symlink": is_symlink,
                "symlink_target": link_target,
                "desc_chars": len(description),
                "desc_tokens": estimate_tokens(description),
                "body_chars": len(body),
                "body_tokens": estimate_tokens(body),
                "total_chars": len(text),
                "total_tokens": estimate_tokens(text),
            })

    if PLUGIN_CACHE.exists():
        for cmd_md in sorted(PLUGIN_CACHE.glob("*/*/*/commands/*.md")):
            plugin = cmd_md.parts[-4]
            cmd_name = cmd_md.stem
            try:
                text = cmd_md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            fm, body = parse_frontmatter(text)
            description = fm.get("description", "").strip()
            if not description:
                for line in body.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        description = s[:200]
                        break
            skills.append({
                "name": f"{plugin}:{cmd_name}",
                "description": description,
                "category": "plugin",
                "source": f"plugins/{plugin}/commands/{cmd_name}",
                "path": str(cmd_md),
                "symlink": False,
                "symlink_target": None,
                "desc_chars": len(description),
                "desc_tokens": estimate_tokens(description),
                "body_chars": len(body),
                "body_tokens": estimate_tokens(body),
                "total_chars": len(text),
                "total_tokens": estimate_tokens(text),
            })

    for name, description in BUILTIN_SKILLS:
        skills.append({
            "name": name,
            "description": description,
            "category": "built-in",
            "source": "shipped with claude-code CLI",
            "path": None,
            "symlink": False,
            "symlink_target": None,
            "desc_chars": len(description),
            "desc_tokens": estimate_tokens(description),
            "body_chars": None,
            "body_tokens": None,
            "total_chars": None,
            "total_tokens": None,
        })

    return skills


def discover_tools() -> list[dict]:
    """Built-in CLI tool descriptions — they live in the same system prompt as skills."""
    tools = []
    for name, description in BUILTIN_TOOLS:
        tools.append({
            "name": name,
            "description": description,
            "category": "tool",
            "source": "shipped with claude-code CLI",
            "path": None,
            "symlink": False,
            "symlink_target": None,
            "desc_chars": len(description),
            "desc_tokens": estimate_tokens(description),
            "body_chars": None,
            "body_tokens": None,
            "total_chars": None,
            "total_tokens": None,
            "canonical": True,
            "duplicate": False,
        })
    return tools


def discover_skill_usage(verbose: bool = False) -> dict[str, dict]:
    """Walk Claude Code session logs and count Skill tool invocations per skill name.

    Returns: { skill_name: { "count": int, "last": iso8601 } }
    """
    counts: dict[str, dict] = {}
    if not SESSIONS_ROOT.exists():
        return counts

    files = list(SESSIONS_ROOT.rglob("*.jsonl"))
    if verbose:
        print(f"  scanning {len(files)} session logs…", file=sys.stderr)

    for jsonl in files:
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"name":"Skill"' not in line and '"name": "Skill"' not in line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = evt.get("message")
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    ts = evt.get("timestamp", "")
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use" or block.get("name") != "Skill":
                            continue
                        skill_name = (block.get("input") or {}).get("skill")
                        if not skill_name:
                            continue
                        rec = counts.setdefault(skill_name, {"count": 0, "last": ""})
                        rec["count"] += 1
                        if ts and ts > rec["last"]:
                            rec["last"] = ts
        except Exception:
            continue
    return counts


CATEGORY_PRIORITY = {"agent-sdk": 0, "user": 1, "plugin": 2, "built-in": 3, "tool": 4}


def annotate_canonical(skills: list[dict]) -> None:
    by_name: dict[str, list[dict]] = {}
    for s in skills:
        by_name.setdefault(s["name"], []).append(s)
    for name, entries in by_name.items():
        symlinked = [s for s in entries if s.get("symlink")]
        non_symlink = [s for s in entries if not s.get("symlink")]
        non_symlink.sort(key=lambda s: CATEGORY_PRIORITY.get(s["category"], 99))
        if symlinked:
            for s in symlinked:
                s["canonical"] = True
                s["duplicate"] = False
            for s in non_symlink:
                s["canonical"] = False
                s["duplicate"] = False
        else:
            for i, s in enumerate(non_symlink):
                s["duplicate"] = len(non_symlink) > 1
                s["canonical"] = i == 0


def attach_usage(skills: list[dict], usage: dict[str, dict]) -> None:
    for s in skills:
        rec = usage.get(s["name"])
        s["invocations"] = rec["count"] if rec else 0
        s["last_invoked"] = rec["last"] if rec else None


# ─── snapshots ────────────────────────────────────────────────────────────

def save_snapshot(skills: list[dict]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tiktoken": TIKTOKEN_AVAILABLE,
        "skills": [{
            "name": s["name"],
            "category": s["category"],
            "canonical": s.get("canonical", False),
            "desc_tokens": s.get("desc_tokens") or 0,
            "body_tokens": s.get("body_tokens") or 0,
        } for s in skills],
    }
    SNAPSHOT_PATH.write_text(json.dumps(payload, indent=2))


def load_snapshot() -> dict | None:
    if not SNAPSHOT_PATH.exists():
        return None
    try:
        return json.loads(SNAPSHOT_PATH.read_text())
    except Exception:
        return None


def diff_against_snapshot(prev: dict | None, current: list[dict]) -> dict:
    """Return a dict describing changes since the last snapshot. Empty dict if no prior."""
    if not prev:
        return {}
    prev_skills = {s["name"]: s for s in prev.get("skills", []) if s.get("canonical")}
    curr_skills = {s["name"]: s for s in current if s.get("canonical")}

    added = sorted(set(curr_skills) - set(prev_skills))
    removed = sorted(set(prev_skills) - set(curr_skills))

    grew = []
    for name, s in curr_skills.items():
        if name in prev_skills:
            old = prev_skills[name].get("desc_tokens", 0)
            new = s.get("desc_tokens", 0)
            if old > 0 and new >= old * 2 and (new - old) >= 20:
                grew.append({"name": name, "old": old, "new": new})

    prev_total = sum(s.get("desc_tokens", 0) for s in prev_skills.values())
    curr_total = sum(s.get("desc_tokens", 0) for s in curr_skills.values())

    return {
        "timestamp": prev.get("timestamp"),
        "added": added,
        "removed": removed,
        "grew": grew,
        "delta_always_loaded": curr_total - prev_total,
        "prev_total": prev_total,
        "curr_total": curr_total,
    }


def print_diff(diff: dict) -> None:
    if not diff:
        return
    if not (diff["added"] or diff["removed"] or diff["grew"] or diff["delta_always_loaded"]):
        return
    print(f"\n  ── since last run ({diff['timestamp']}) ──", file=sys.stderr)
    delta = diff["delta_always_loaded"]
    if delta:
        sign = "+" if delta > 0 else ""
        arrow = "↑" if delta > 0 else "↓"
        print(f"  {arrow} always-loaded: {sign}{delta} tok ({diff['prev_total']} → {diff['curr_total']})", file=sys.stderr)
    if diff["added"]:
        sample = ", ".join(diff["added"][:5])
        more = f" (+{len(diff['added']) - 5} more)" if len(diff["added"]) > 5 else ""
        print(f"  + {len(diff['added'])} new: {sample}{more}", file=sys.stderr)
    if diff["removed"]:
        sample = ", ".join(diff["removed"][:5])
        more = f" (+{len(diff['removed']) - 5} more)" if len(diff["removed"]) > 5 else ""
        print(f"  - {len(diff['removed'])} removed: {sample}{more}", file=sys.stderr)
    if diff["grew"]:
        print(f"  ↑ {len(diff['grew'])} grew >2x:", file=sys.stderr)
        for g in diff["grew"]:
            print(f"      {g['name']}: {g['old']} → {g['new']} tok", file=sys.stderr)


# ─── --fix-dupes ──────────────────────────────────────────────────────────

def find_dupe_candidates(skills: list[dict]) -> list[tuple[str, Path, Path]]:
    """Return [(name, user_dir, agent_dir)] for byte-identical user/agent-sdk pairs."""
    user_skills = {s["name"]: s for s in skills if s["category"] == "user" and not s.get("symlink") and s.get("path")}
    agent_skills = {s["name"]: s for s in skills if s["category"] == "agent-sdk" and s.get("path")}
    actions = []
    for name, user_s in user_skills.items():
        agent_s = agent_skills.get(name)
        if not agent_s:
            continue
        user_md = Path(user_s["path"])
        agent_md = Path(agent_s["path"])
        try:
            if user_md.read_bytes() == agent_md.read_bytes():
                actions.append((name, user_md.parent, agent_md.parent))
        except Exception:
            continue
    return actions


def fix_duplicates(skills: list[dict], apply: bool) -> int:
    actions = find_dupe_candidates(skills)
    if not actions:
        print("No byte-identical disk duplicates found between ~/.claude/skills and ~/.agents/skills.")
        return 0
    verb = "Replacing" if apply else "Would replace"
    print(f"{verb} {len(actions)} duplicate{'s' if len(actions) != 1 else ''} with symlinks to the SDK-managed copy:\n")
    for name, user_dir, agent_dir in actions:
        print(f"  {name}")
        print(f"    {user_dir}  ─→  {agent_dir}")
        if apply:
            shutil.rmtree(user_dir)
            os.symlink(str(agent_dir), str(user_dir))
    if apply:
        print(f"\n✓ {len(actions)} symlinks created.")
    else:
        print(f"\nDry run. Re-run with --apply to actually do it:")
        print(f"  skillchart --fix-dupes --apply")
    return len(actions)


# ─── output ────────────────────────────────────────────────────────────────

def default_output_path() -> Path:
    return default_cache_dir() / "dashboard.html"


def open_in_browser(path: Path) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=False)
        elif sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skill Chart — Claude Code skills on this machine</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --panel-2: #1f2630; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --warn: #f0883e;
    --hot: #f85149; --good: #3fb950;
    --user: #d2a8ff; --agent: #79c0ff; --plugin: #ffa657; --builtin: #7ee787; --tool: #ff7b72;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; }
  header { padding: 24px 28px 16px; border-bottom: 1px solid var(--border); }
  h1 { margin: 0 0 6px; font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  .sub code { background: var(--panel); padding: 1px 5px; border-radius: 3px; font-size: 11.5px; }
  main { padding: 20px 28px 60px; max-width: 1500px; margin: 0 auto; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 22px; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .stat .v { font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }
  .stat .l { color: var(--muted); font-size: 12px; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat.warn .v { color: var(--warn); }
  .stat.hot .v { color: var(--hot); }
  .stat.good .v { color: var(--good); }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; align-items: center; }
  .controls input[type=search] { flex: 1; min-width: 260px; background: var(--panel); border: 1px solid var(--border); color: var(--text); padding: 9px 12px; border-radius: 8px; font: inherit; }
  .controls input[type=search]:focus { outline: none; border-color: var(--accent); }
  .pill { background: var(--panel); border: 1px solid var(--border); color: var(--text); padding: 7px 12px; border-radius: 999px; font-size: 12.5px; cursor: pointer; user-select: none; }
  .pill.active { background: var(--accent); border-color: var(--accent); color: #0d1117; font-weight: 600; }
  .pill:hover:not(.active) { border-color: var(--accent); }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  th { background: var(--panel-2); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); cursor: pointer; user-select: none; position: sticky; top: 0; }
  th:hover { color: var(--text); }
  th .arrow { opacity: 0.4; margin-left: 4px; }
  th.sorted .arrow { opacity: 1; color: var(--accent); }
  tbody tr { transition: background 0.1s; }
  tbody tr:hover { background: var(--panel-2); }
  td.name { font-weight: 600; }
  td.desc { color: var(--muted); font-size: 13px; max-width: 460px; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
  .bar { display: inline-block; height: 6px; border-radius: 3px; background: var(--accent); vertical-align: middle; margin-left: 6px; min-width: 2px; opacity: 0.7; }
  .bar.body { background: var(--warn); }
  .cat { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .cat.user { background: rgba(210, 168, 255, 0.15); color: var(--user); }
  .cat.agent-sdk { background: rgba(121, 192, 255, 0.15); color: var(--agent); }
  .cat.plugin { background: rgba(255, 166, 87, 0.15); color: var(--plugin); }
  .cat.built-in { background: rgba(126, 231, 135, 0.15); color: var(--builtin); }
  .cat.tool { background: rgba(255, 123, 114, 0.15); color: var(--tool); }
  .dup-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; background: rgba(248, 81, 73, 0.18); color: var(--hot); margin-left: 6px; vertical-align: middle; letter-spacing: 0.05em; }
  .never-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; background: rgba(240, 136, 62, 0.18); color: var(--warn); margin-left: 6px; vertical-align: middle; letter-spacing: 0.05em; }
  .empty { padding: 60px 20px; text-align: center; color: var(--muted); }
  details#opt-panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 18px; margin-bottom: 22px; }
  details#opt-panel summary { cursor: pointer; font-weight: 600; padding: 4px 0; user-select: none; }
  details#opt-panel summary:hover { color: var(--accent); }
  .opt-section { margin-top: 14px; }
  .opt-section h3 { margin: 0 0 6px; font-size: 13px; color: var(--accent); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .opt-section p { margin: 0 0 8px; color: var(--muted); font-size: 13px; }
  .opt-list { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.7; }
  .opt-list code { background: var(--panel-2); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  .opt-list .tok { color: var(--warn); font-variant-numeric: tabular-nums; }
  .savings { display: inline-block; background: rgba(63, 185, 80, 0.15); color: var(--good); padding: 3px 10px; border-radius: 6px; font-weight: 600; font-size: 12px; margin-top: 6px; }
  .meta { font-size: 11.5px; color: var(--muted); margin-top: 14px; line-height: 1.6; }
  .meta code { background: var(--panel); padding: 1px 5px; border-radius: 3px; font-size: 11px; }
  .diff-banner { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; font-size: 13px; }
  .diff-banner .delta-up { color: var(--hot); }
  .diff-banner .delta-down { color: var(--good); }
  .budget { background: linear-gradient(135deg, rgba(88,166,255,0.08), rgba(88,166,255,0.02)); border: 1px solid var(--border); border-radius: 10px; padding: 16px 20px; margin-bottom: 22px; }
  .budget-head { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
  .budget-head .plan { font-size: 16px; font-weight: 600; }
  .budget-head .plan code { background: var(--panel-2); padding: 2px 7px; border-radius: 4px; font-size: 12px; color: var(--accent); margin-left: 6px; }
  .budget-head .plan-hint { color: var(--muted); font-size: 12px; }
  .budget-bar-row { display: flex; align-items: center; gap: 10px; margin: 8px 0; font-size: 13px; }
  .budget-bar-row .lbl { width: 130px; color: var(--muted); font-size: 12px; flex-shrink: 0; }
  .budget-bar-row .val { font-variant-numeric: tabular-nums; min-width: 220px; }
  .budget-bar-row .val .num { font-weight: 600; color: var(--text); }
  .budget-bar-row .val .pct { color: var(--warn); margin-left: 6px; font-size: 12px; }
  .budget-bar { flex: 1; height: 8px; background: var(--panel-2); border-radius: 4px; overflow: hidden; min-width: 120px; }
  .budget-bar > span { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--warn)); }
  .budget-bar.skills > span { background: linear-gradient(90deg, var(--user), var(--accent)); }
  .budget-bar.tools > span { background: linear-gradient(90deg, var(--tool), var(--warn)); }
  .budget-aside { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
  .budget-aside .item { font-size: 12px; }
  .budget-aside .item .k { color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; font-size: 10.5px; }
  .budget-aside .item .v { font-weight: 600; font-size: 14px; margin-top: 2px; }
  .budget-aside .item .v small { color: var(--muted); font-weight: 400; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>Skill Chart</h1>
  <div class="sub">All Claude Code skills + tools on this machine and the context they cost you. <span id="counter-mode"></span></div>
</header>
<main>

  <div id="diff-banner-host"></div>

  <div class="stats" id="stats"></div>

  <section id="budget" class="budget"></section>

  <details id="opt-panel" open>
    <summary>💡 Optimization suggestions</summary>
    <div id="opt-body"></div>
  </details>

  <div class="controls">
    <input type="search" id="q" placeholder="Search skills + tools…" autofocus>
    <button class="pill active" data-cat="all">All</button>
    <button class="pill" data-cat="user">User</button>
    <button class="pill" data-cat="agent-sdk">Agent SDK</button>
    <button class="pill" data-cat="plugin">Plugin</button>
    <button class="pill" data-cat="built-in">Built-in</button>
    <button class="pill" data-cat="tool">Tools</button>
    <button class="pill" data-cat="never">⚠ Never used</button>
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th data-sort="name">Skill / Tool <span class="arrow">↕</span></th>
        <th data-sort="category">Source <span class="arrow">↕</span></th>
        <th data-sort="description">Description <span class="arrow">↕</span></th>
        <th data-sort="invocations" class="num">Invocations<br><span style="font-weight:400;text-transform:none;font-size:10.5px;letter-spacing:0;">all sessions</span> <span class="arrow">↕</span></th>
        <th data-sort="desc_tokens" class="num">Always loaded<br><span style="font-weight:400;text-transform:none;font-size:10.5px;letter-spacing:0;">tokens / turn</span> <span class="arrow">↕</span></th>
        <th data-sort="body_tokens" class="num">When invoked<br><span style="font-weight:400;text-transform:none;font-size:10.5px;letter-spacing:0;">tokens (full body)</span> <span class="arrow">↕</span></th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

  <div class="meta">
    Token estimate: <span id="counter-mode-2"></span>. Always-loaded = description in the system prompt every turn. When-invoked = the SKILL.md body pulled when Claude calls the skill. Built-in skills and CLI tools ship inside Claude Code; their bodies don't live on disk and aren't measured here.
    Skills with <span class="never-badge">NEVER</span> have zero recorded invocations across all your local session logs (<code>~/.claude/projects/**/*.jsonl</code>) — strong candidate for archival.
  </div>

</main>

<script>
const SKILLS = __DATA__;
const META = __META__;

const tbody = document.getElementById("tbody");
const q = document.getElementById("q");
const statsEl = document.getElementById("stats");
const optEl = document.getElementById("opt-body");
const diffHost = document.getElementById("diff-banner-host");
const budgetEl = document.getElementById("budget");

document.getElementById("counter-mode").textContent = `· Tokens via ${META.tiktoken ? "tiktoken (cl100k_base)" : "chars/4 estimate"}`;
document.getElementById("counter-mode-2").textContent = META.tiktoken ? "tiktoken cl100k_base (accurate)" : "chars/4 (rough)";

// Diff banner
if (META.diff && (META.diff.delta_always_loaded || META.diff.added.length || META.diff.removed.length || META.diff.grew.length)) {
  const d = META.diff;
  const parts = [];
  if (d.delta_always_loaded) {
    const cls = d.delta_always_loaded > 0 ? "delta-up" : "delta-down";
    const arrow = d.delta_always_loaded > 0 ? "↑" : "↓";
    parts.push(`<span class="${cls}">${arrow} always-loaded ${d.delta_always_loaded > 0 ? "+" : ""}${d.delta_always_loaded} tok</span>`);
  }
  if (d.added.length) parts.push(`+${d.added.length} new`);
  if (d.removed.length) parts.push(`-${d.removed.length} removed`);
  if (d.grew.length) parts.push(`${d.grew.length} grew >2x`);
  diffHost.innerHTML = `<div class="diff-banner">📊 Since last snapshot <span style="color:var(--muted)">(${d.timestamp})</span>: ${parts.join(" · ")}</div>`;
}

let activeCat = "all";
let sortKey = "desc_tokens";
let sortDir = -1;

function fmt(n) { return n == null ? "—" : n.toLocaleString(); }
function maxBy(arr, k) { return arr.reduce((m, x) => Math.max(m, x[k] || 0), 0); }

function renderBudget() {
  const plan = META.plan;
  const planKey = META.plan_key;
  const canonical = SKILLS.filter(s => s.canonical);
  const skillRows = canonical.filter(x => x.category !== "tool");
  const toolRows = canonical.filter(x => x.category === "tool");
  const skillsAlways = skillRows.reduce((s, x) => s + (x.desc_tokens || 0), 0);
  const toolsAlways = toolRows.reduce((s, x) => s + (x.desc_tokens || 0), 0);
  const totalAlways = skillsAlways + toolsAlways;
  const ctx = plan.context_window;
  const pctSkills = (skillsAlways / ctx) * 100;
  const pctTools = (toolsAlways / ctx) * 100;
  const pctTotal = pctSkills + pctTools;

  const msgPer5h = plan.messages_per_5h;
  const msgsPerWeek = msgPer5h * (24 * 7 / 5);
  const skillsPerWindow = totalAlways * msgPer5h;
  const skillsPerMonth = totalAlways * msgsPerWeek * 4.345;

  // What it would cost if you were on API pricing (Sonnet input ~$3/M, Opus ~$15/M).
  // We assume cache hit ~70% of the time (5-min TTL, multi-turn sessions).
  const cacheHitRate = 0.70;
  const sonnetUSDperM = 3.00;
  const opusUSDperM = 15.00;
  const billable = skillsPerMonth * (1 - cacheHitRate);
  const equivSonnet = (billable / 1_000_000) * sonnetUSDperM;
  const equivOpus = (billable / 1_000_000) * opusUSDperM;

  const opusLine = plan.code_hours_opus_weekly
    ? `· Opus: ${plan.code_hours_opus_weekly[0]}–${plan.code_hours_opus_weekly[1]} h/wk`
    : "";

  budgetEl.innerHTML = `
    <div class="budget-head">
      <div class="plan">📊 Budget · <span style="color:var(--muted);font-weight:400;">on</span> <code>${escape(plan.label)}</code></div>
      <div class="plan-hint">~${msgPer5h} msgs / 5h window · Sonnet ${plan.code_hours_sonnet_weekly[0]}–${plan.code_hours_sonnet_weekly[1]} h/wk ${opusLine}</div>
    </div>

    <div class="budget-bar-row">
      <span class="lbl">Skills / turn</span>
      <span class="val"><span class="num">${fmt(skillsAlways)}</span> tok <span class="pct">${pctSkills.toFixed(2)}%</span></span>
      <span class="budget-bar skills"><span style="width:${Math.min(100, pctSkills * 6).toFixed(1)}%"></span></span>
    </div>
    <div class="budget-bar-row">
      <span class="lbl">CLI tools / turn</span>
      <span class="val"><span class="num">${fmt(toolsAlways)}</span> tok <span class="pct">${pctTools.toFixed(2)}%</span></span>
      <span class="budget-bar tools"><span style="width:${Math.min(100, pctTools * 6).toFixed(1)}%"></span></span>
    </div>
    <div class="budget-bar-row">
      <span class="lbl"><strong>Total / turn</strong></span>
      <span class="val"><span class="num">${fmt(totalAlways)}</span> tok of ${fmt(ctx)} <span class="pct">${pctTotal.toFixed(2)}%</span></span>
      <span class="budget-bar"><span style="width:${Math.min(100, pctTotal * 6).toFixed(1)}%"></span></span>
    </div>

    <div class="budget-aside">
      <div class="item">
        <div class="k">Per 5h window</div>
        <div class="v">~${fmt(Math.round(skillsPerWindow / 1000))}k tok <small>(${msgPer5h} turns × ${fmt(totalAlways)})</small></div>
      </div>
      <div class="item">
        <div class="k">Per month at cap</div>
        <div class="v">~${fmt(Math.round(skillsPerMonth / 1_000_000))}M tok <small>(at max msgs/5h, 24×7)</small></div>
      </div>
      <div class="item">
        <div class="k">API-equivalent / mo</div>
        <div class="v">~$${equivSonnet.toFixed(2)} <small>Sonnet</small> · ~$${equivOpus.toFixed(2)} <small>Opus</small></div>
      </div>
      <div class="item">
        <div class="k">Plan</div>
        <div class="v">$${plan.monthly_price_usd}/mo <small>flat — no per-token billing</small></div>
      </div>
    </div>
  `;
}

function renderStats(rows) {
  const canonical = rows.filter(x => x.canonical);
  const skillRows = canonical.filter(x => x.category !== "tool");
  const toolRows = canonical.filter(x => x.category === "tool");
  const skillsAlways = skillRows.reduce((s, x) => s + (x.desc_tokens || 0), 0);
  const toolsAlways = toolRows.reduce((s, x) => s + (x.desc_tokens || 0), 0);
  const onDemandSum = canonical.reduce((s, x) => s + (x.body_tokens || 0), 0);
  const heaviest = canonical.reduce((m, x) => (x.body_tokens || 0) > (m?.body_tokens || 0) ? x : m, null);
  const dupes = rows.filter(x => x.duplicate && !x.canonical).length;
  const never = canonical.filter(x => x.invocations === 0 && (x.category === "user" || x.category === "agent-sdk" || x.category === "plugin")).length;

  statsEl.innerHTML = `
    <div class="stat"><div class="v">${skillRows.length}</div><div class="l">Unique skills</div></div>
    <div class="stat warn"><div class="v">~${fmt(skillsAlways)}</div><div class="l">Skill tokens / turn</div></div>
    <div class="stat warn"><div class="v">~${fmt(toolsAlways)}</div><div class="l">Tool tokens / turn</div></div>
    <div class="stat"><div class="v">~${fmt(onDemandSum)}</div><div class="l">Tokens if every body fires</div></div>
    <div class="stat hot"><div class="v">${heaviest ? heaviest.name : "—"}</div><div class="l">Heaviest body (~${fmt(heaviest?.body_tokens)} tok)</div></div>
    ${never ? `<div class="stat hot"><div class="v">${never}</div><div class="l">Never invoked</div></div>` : ""}
    ${dupes ? `<div class="stat hot"><div class="v">${dupes}</div><div class="l">Redundant disk copies</div></div>` : ""}
  `;
}

function renderOptimizations() {
  const canonical = SKILLS.filter(s => s.canonical);
  const editable = canonical.filter(s => s.category === "user" || s.category === "agent-sdk");
  const heavyDesc = [...editable].sort((a, b) => b.desc_tokens - a.desc_tokens).slice(0, 5);
  const dupes = SKILLS.filter(s => s.duplicate && !s.canonical);
  const TARGET_DESC = 50;
  const trimSavings = heavyDesc.reduce((sum, s) => sum + Math.max(0, s.desc_tokens - TARGET_DESC), 0);
  const never = canonical.filter(s => (s.category === "user" || s.category === "agent-sdk" || s.category === "plugin") && s.invocations === 0)
                         .sort((a, b) => (b.desc_tokens + (b.body_tokens || 0)) - (a.desc_tokens + (a.body_tokens || 0)))
                         .slice(0, 8);

  const sections = [];

  if (dupes.length) {
    sections.push(`
      <div class="opt-section">
        <h3>🗑️ ${dupes.length} redundant disk copies</h3>
        <p>Real directories duplicated across <code>~/.claude/skills/</code> and <code>~/.agents/skills/</code>. Run <code>skillchart --fix-dupes --apply</code> to symlink them into a single canonical source.</p>
        <ul class="opt-list">
          ${dupes.map(s => `<li><code>${escape(s.name)}</code> — <span class="tok">${fmt(s.body_tokens)} body tok</span> wasted on disk</li>`).join("")}
        </ul>
      </div>
    `);
  }

  if (never.length) {
    sections.push(`
      <div class="opt-section">
        <h3>👻 ${never.length} skill${never.length === 1 ? "" : "s"} never invoked</h3>
        <p>Zero recorded calls across all your session logs. Each one still costs always-loaded tokens. Archive (<code>mv ~/.claude/skills/&lt;name&gt; ~/.claude/skills.archive/</code>) the ones you don't expect to use.</p>
        <ul class="opt-list">
          ${never.map(s => `<li><code>${escape(s.name)}</code> — <span class="tok">${fmt(s.desc_tokens)} desc + ${fmt(s.body_tokens || 0)} body tok</span></li>`).join("")}
        </ul>
        <span class="savings">Potential save: ~${fmt(never.reduce((sum, s) => sum + s.desc_tokens, 0))} tok / turn</span>
      </div>
    `);
  }

  sections.push(`
    <div class="opt-section">
      <h3>✂️ Top 5 heaviest descriptions (always-loaded burden)</h3>
      <p>Every line below is injected into your system prompt on <em>every</em> turn. Trim each to ~${TARGET_DESC} tokens (Anthropic's skill-author guidance) and you save ~<strong>${fmt(trimSavings)} tokens per turn</strong>.</p>
      <ul class="opt-list">
        ${heavyDesc.map(s => `<li><code>${escape(s.name)}</code> — <span class="tok">${fmt(s.desc_tokens)} tok</span> · ${escape(truncate(s.description, 110))}</li>`).join("")}
      </ul>
      <span class="savings">Potential save: ~${fmt(trimSavings)} tok / turn</span>
    </div>
  `);

  optEl.innerHTML = sections.join("");
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function truncate(s, n) { s = s || ""; return s.length > n ? s.slice(0, n) + "…" : s; }

function render() {
  const term = q.value.trim().toLowerCase();
  let rows = SKILLS.filter(s => {
    if (activeCat === "never") {
      if (!(s.invocations === 0 && (s.category === "user" || s.category === "agent-sdk" || s.category === "plugin"))) return false;
    } else if (activeCat !== "all" && s.category !== activeCat) return false;
    if (!term) return true;
    return (s.name + " " + s.description + " " + s.source).toLowerCase().includes(term);
  });

  rows.sort((a, b) => {
    const va = a[sortKey], vb = b[sortKey];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string") return sortDir * va.localeCompare(vb);
    return sortDir * (va - vb);
  });

  renderBudget();
  renderStats(rows);
  renderOptimizations();

  const maxDesc = maxBy(rows, "desc_tokens") || 1;
  const maxBody = maxBy(rows, "body_tokens") || 1;
  const maxInv = maxBy(rows, "invocations") || 1;

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">No skills match.</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((s, i) => {
    const descBar = Math.max(2, Math.round(((s.desc_tokens || 0) / maxDesc) * 70));
    const bodyBar = s.body_tokens != null ? Math.max(2, Math.round((s.body_tokens / maxBody) * 70)) : 0;
    const invBar = s.invocations > 0 ? Math.max(2, Math.round((s.invocations / maxInv) * 50)) : 0;
    const isUserSkill = s.category === "user" || s.category === "agent-sdk" || s.category === "plugin";
    const neverBadge = (isUserSkill && s.invocations === 0) ? ' <span class="never-badge">NEVER</span>' : '';
    return `
      <tr data-i="${i}">
        <td class="name">${escape(s.name)}${neverBadge}${s.duplicate && !s.canonical ? ' <span class="dup-badge">DUP</span>' : ''}</td>
        <td><span class="cat ${s.category}">${s.category}</span></td>
        <td class="desc" title="${escape(s.description)}">${escape(truncate(s.description, 130))}</td>
        <td class="num">${s.invocations > 0 ? `${fmt(s.invocations)}<span class="bar" style="width:${invBar}px"></span>` : (isUserSkill ? '<span style="color:var(--warn)">0</span>' : '<span style="color:var(--muted)">—</span>')}</td>
        <td class="num">${fmt(s.desc_tokens)}<span class="bar" style="width:${descBar}px"></span></td>
        <td class="num">${s.body_tokens == null ? '<span style="color:var(--muted)">built-in</span>' : `${fmt(s.body_tokens)}<span class="bar body" style="width:${bodyBar}px"></span>`}</td>
      </tr>
      <tr class="detail-row" data-i="${i}" style="display:none;">
        <td colspan="6" style="background:#0a0e13; padding:14px 18px; font-size:13px;">
          <div style="font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);margin-bottom:8px;word-break:break-all;">${escape(s.path || s.source)}</div>
          <div style="white-space:pre-wrap;line-height:1.55;">${escape(s.description)}</div>
          ${s.last_invoked ? `<div style="margin-top:10px;font-size:11.5px;color:var(--muted);">Last invoked: ${escape(s.last_invoked)}</div>` : ''}
        </td>
      </tr>
    `;
  }).join("");

  tbody.querySelectorAll("tr[data-i]:not(.detail-row)").forEach(tr => {
    tr.addEventListener("click", () => {
      const i = tr.dataset.i;
      const detail = tbody.querySelector(`tr.detail-row[data-i="${i}"]`);
      const open = detail.style.display === "table-row";
      detail.style.display = open ? "none" : "table-row";
    });
  });

  document.querySelectorAll("th[data-sort]").forEach(th => {
    th.classList.toggle("sorted", th.dataset.sort === sortKey);
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.textContent = th.dataset.sort === sortKey ? (sortDir === -1 ? "↓" : "↑") : "↕";
  });
}

document.querySelectorAll(".pill[data-cat]").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".pill[data-cat]").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    activeCat = b.dataset.cat;
    render();
  });
});

document.querySelectorAll("th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = (k === "name" || k === "category" || k === "description") ? 1 : -1; }
    render();
  });
});

q.addEventListener("input", render);
render();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(
        description="Audit Claude Code skills + tools on this machine and emit an HTML dashboard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-o", "--output", type=Path, help="Output path for the HTML dashboard.")
    ap.add_argument("--json", action="store_true", help="Print scanned skill data as JSON to stdout.")
    ap.add_argument("--no-open", action="store_true", help="Don't open the dashboard in the browser.")
    ap.add_argument("--fix-dupes", action="store_true", help="Find byte-identical disk duplicates and offer to symlink them.")
    ap.add_argument("--apply", action="store_true", help="With --fix-dupes: actually perform the symlinking (default is dry-run).")
    ap.add_argument("--no-snapshot", action="store_true", help="Don't write a snapshot for next-run diffing.")
    ap.add_argument("--no-usage", action="store_true", help="Skip parsing session logs for invocation counts (faster).")
    ap.add_argument("--plan", choices=list(PLANS.keys()), help="Override subscription plan for this run (free / pro / max-5x / max-20x).")
    ap.add_argument("--set-plan", choices=list(PLANS.keys()), metavar="PLAN", help="Save plan to ~/.config/skillchart/config.json and exit.")
    ap.add_argument("--show-config", action="store_true", help="Print the resolved config and exit.")
    ap.add_argument("--version", action="version", version=f"skillchart {__version__}")
    args = ap.parse_args()

    cfg = load_config()
    if args.set_plan:
        cfg["plan"] = args.set_plan
        save_config(cfg)
        print(f"saved plan={args.set_plan} to {CONFIG_PATH}")
        return
    if args.show_config:
        resolved = {"plan": args.plan or cfg.get("plan", "max-5x"), "config_path": str(CONFIG_PATH), "config_exists": CONFIG_PATH.exists()}
        print(json.dumps(resolved, indent=2))
        return
    plan_key = args.plan or cfg.get("plan", "max-5x")
    plan = PLANS.get(plan_key, PLANS["max-5x"])

    skills = discover_skills()

    if args.fix_dupes:
        annotate_canonical(skills)
        n = fix_duplicates(skills, apply=args.apply)
        sys.exit(0 if n == 0 or args.apply else 1)

    tools = discover_tools()
    annotate_canonical(skills)
    all_rows = skills + tools

    usage = {} if args.no_usage else discover_skill_usage(verbose=not args.json)
    attach_usage(all_rows, usage)

    prev = load_snapshot()
    diff = diff_against_snapshot(prev, all_rows)

    all_rows.sort(key=lambda s: (-(s.get("body_tokens") or 0), s["name"]))

    if args.json:
        json.dump(all_rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    out_path = args.output or default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "tiktoken": TIKTOKEN_AVAILABLE,
        "diff": diff or None,
        "version": __version__,
        "plan_key": plan_key,
        "plan": plan,
    }
    out = (HTML_TEMPLATE
           .replace("__DATA__", json.dumps(all_rows, ensure_ascii=False))
           .replace("__META__", json.dumps(meta, ensure_ascii=False)))
    out_path.write_text(out, encoding="utf-8")
    print(f"wrote {out_path}  ({len(skills)} skills, {len(tools)} tools)", file=sys.stderr)

    canonical = [s for s in all_rows if s["canonical"]]
    skill_canon = [s for s in canonical if s["category"] != "tool"]
    tool_canon = [s for s in canonical if s["category"] == "tool"]
    by_cat: dict[str, int] = {}
    for s in skill_canon:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    skills_desc = sum(s["desc_tokens"] or 0 for s in skill_canon)
    skills_body = sum(s["body_tokens"] or 0 for s in skill_canon)
    tools_desc = sum(s["desc_tokens"] or 0 for s in tool_canon)
    invoked = sum(1 for s in canonical if s.get("invocations", 0) > 0)
    never = sum(1 for s in canonical if s.get("invocations", 0) == 0 and s["category"] in ("user", "agent-sdk", "plugin"))
    print(f"  unique skills: {len(skill_canon)} ({by_cat}), {len(tool_canon)} tools", file=sys.stderr)
    always_total = skills_desc + tools_desc
    pct = always_total / plan["context_window"] * 100
    print(f"  always-loaded: ~{skills_desc:,} skill tok + ~{tools_desc:,} tool tok = ~{always_total:,} tok / turn ({pct:.1f}% of {plan['context_window']:,} ctx — {plan['label']})", file=sys.stderr)
    print(f"  on-demand sum: ~{skills_body:,} tokens", file=sys.stderr)
    print(f"  usage: {invoked} invoked at least once, {never} never invoked", file=sys.stderr)
    if not TIKTOKEN_AVAILABLE:
        print(f"  (tip: pip install tiktoken for accurate counts)", file=sys.stderr)

    print_diff(diff)

    if not args.no_snapshot:
        save_snapshot(all_rows)

    if not args.no_open:
        open_in_browser(out_path)


if __name__ == "__main__":
    main()
