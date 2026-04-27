#!/usr/bin/env python3
"""Scan every Claude Code skill on this machine and emit a self-contained HTML dashboard."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path.home()

SKILL_ROOTS = [
    (HOME / ".claude" / "skills", "user", "~/.claude/skills"),
    (HOME / ".agents" / "skills", "agent-sdk", "~/.agents/skills"),
]
PLUGIN_CACHE = HOME / ".claude" / "plugins" / "cache"

# Built-in skills shipped with the Claude Code CLI (no SKILL.md on disk).
# Descriptions copied from the system-reminder available-skills listing.
BUILTIN_SKILLS = [
    ("update-config", "Use this skill to configure the Claude Code harness via settings.json. Automated behaviors (\"from now on when X\", \"each time X\", \"whenever X\", \"before/after X\") require hooks configured in settings.json - the harness executes these, not Claude, so memory/preferences cannot fulfill them. Also use for: permissions (\"allow X\", \"add permission\", \"move permission to\"), env vars (\"set X=Y\"), hook troubleshooting, or any changes to settings.json/settings.local.json files. Examples: \"allow npm commands\", \"add bq permission to global settings\", \"move permission to user settings\", \"set DEBUG=true\", \"when claude stops show X\". For simple settings like theme/model, suggest the /config command."),
    ("keybindings-help", "Use when the user wants to customize keyboard shortcuts, rebind keys, add chord bindings, or modify ~/.claude/keybindings.json. Examples: \"rebind ctrl+s\", \"add a chord shortcut\", \"change the submit key\", \"customize keybindings\"."),
    ("simplify", "Review changed code for reuse, quality, and efficiency, then fix any issues found."),
    ("fewer-permission-prompts", "Scan your transcripts for common read-only Bash and MCP tool calls, then add a prioritized allowlist to project .claude/settings.json to reduce permission prompts."),
    ("loop", "Run a prompt or slash command on a recurring interval (e.g. /loop 5m /foo). Omit the interval to let the model self-pace. - When the user wants to set up a recurring task, poll for status, or run something repeatedly on an interval (e.g. \"check the deploy every 5 minutes\", \"keep running /babysit-prs\"). Do NOT invoke for one-off tasks."),
    ("schedule", "Create, update, list, or run scheduled remote agents (routines) on a cron schedule or once at a specific time. - When the user wants to schedule a recurring or one-time remote agent (\"run this every Monday\", \"open a cleanup PR for X in 2 weeks\"), or to manage existing routines. ALSO OFFER PROACTIVELY: after you finish work that has a natural future follow-up, end your reply with a one-line offer to schedule a background agent to do it. Strong signals: a feature flag / gate / experiment / staged rollout was just shipped (offer a one-time agent in ~2 weeks to open a cleanup PR or evaluate results), a new alert/monitor was created (offer a recurring agent to triage it), a TODO/migration with a \"remove once X\" condition was left behind (offer a one-time agent to do the removal). Skip the offer for refactors, bug fixes, and anything that is done once it ships. Name a concrete action and cadence (\"in 2 weeks\", \"every Monday\") and only offer when the run just succeeded — do not pitch a schedule for something that has not happened yet."),
    ("claude-api", "Build, debug, and optimize Claude API / Anthropic SDK apps. Apps built with this skill should include prompt caching. Also handles migrating existing Claude API code between Claude model versions (4.5 → 4.6, 4.6 → 4.7, retired-model replacements). TRIGGER when: code imports `anthropic`/`@anthropic-ai/sdk`; user asks for the Claude API, Anthropic SDK, or Managed Agents; user adds/modifies/tunes a Claude feature (caching, thinking, compaction, tool use, batch, files, citations, memory) or model (Opus/Sonnet/Haiku) in a file; questions about prompt caching / cache hit rate in an Anthropic SDK project. SKIP: file imports `openai`/other-provider SDK, filename like `*-openai.py`/`*-generic.py`, provider-neutral code, general programming/ML."),
    ("savings", "Audit this session's token usage and flag context-eating patterns before they tank the budget."),
    ("init", "Initialize a new CLAUDE.md file with codebase documentation"),
    ("review", "Review a pull request"),
    ("security-review", "Complete a security review of the pending changes on the current branch"),
]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter without external deps. Handles single-line and block-scalar (>) descriptions."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
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
            # block scalar: collect indented continuation lines
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
    """Rough estimate: ~4 chars per token for English+code."""
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
            text = skill_md.read_text(encoding="utf-8", errors="replace")
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

    # Plugin commands (each command file is essentially a skill body)
    if PLUGIN_CACHE.exists():
        for cmd_md in sorted(PLUGIN_CACHE.glob("*/*/*/commands/*.md")):
            # cmd_md = ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/commands/<name>.md
            plugin = cmd_md.parts[-4]
            cmd_name = cmd_md.stem
            text = cmd_md.read_text(encoding="utf-8", errors="replace")
            fm, body = parse_frontmatter(text)
            description = fm.get("description", "").strip()
            if not description:
                # fallback: first non-empty line
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
                "desc_chars": len(description),
                "desc_tokens": estimate_tokens(description),
                "body_chars": len(body),
                "body_tokens": estimate_tokens(body),
                "total_chars": len(text),
                "total_tokens": estimate_tokens(text),
            })

    # Built-in skills: no body on disk
    for name, description in BUILTIN_SKILLS:
        skills.append({
            "name": name,
            "description": description,
            "category": "built-in",
            "source": "shipped with claude-code CLI",
            "path": None,
            "desc_chars": len(description),
            "desc_tokens": estimate_tokens(description),
            "body_chars": None,
            "body_tokens": None,
            "total_chars": None,
            "total_tokens": None,
        })

    return skills


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skill Chart — Claude Code skills on this machine</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-2: #1f2630;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --warn: #f0883e;
    --hot: #f85149;
    --good: #3fb950;
    --user: #d2a8ff;
    --agent: #79c0ff;
    --plugin: #ffa657;
    --builtin: #7ee787;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; }
  header { padding: 24px 28px 16px; border-bottom: 1px solid var(--border); }
  h1 { margin: 0 0 6px; font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 13px; }
  main { padding: 20px 28px 60px; max-width: 1400px; margin: 0 auto; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 22px; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .stat .v { font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }
  .stat .l { color: var(--muted); font-size: 12px; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat.warn .v { color: var(--warn); }
  .stat.hot .v { color: var(--hot); }
  .stat.good .v { color: var(--good); }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; align-items: center; }
  .controls input[type=search] {
    flex: 1; min-width: 260px;
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 9px 12px; border-radius: 8px; font: inherit;
  }
  .controls input[type=search]:focus { outline: none; border-color: var(--accent); }
  .pill {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 7px 12px; border-radius: 999px; font-size: 12.5px; cursor: pointer; user-select: none;
  }
  .pill.active { background: var(--accent); border-color: var(--accent); color: #0d1117; font-weight: 600; }
  .pill:hover:not(.active) { border-color: var(--accent); }
  select.pill { appearance: none; padding-right: 28px; background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%); background-position: calc(100% - 14px) 50%, calc(100% - 9px) 50%; background-size: 5px 5px, 5px 5px; background-repeat: no-repeat; }
  table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  th { background: var(--panel-2); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); cursor: pointer; user-select: none; position: sticky; top: 0; }
  th:hover { color: var(--text); }
  th .arrow { opacity: 0.4; margin-left: 4px; }
  th.sorted .arrow { opacity: 1; color: var(--accent); }
  tbody tr { transition: background 0.1s; }
  tbody tr:hover { background: var(--panel-2); }
  tbody tr.expanded { background: var(--panel-2); }
  td.name { font-weight: 600; }
  td.desc { color: var(--muted); font-size: 13px; max-width: 520px; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
  .bar { display: inline-block; height: 6px; border-radius: 3px; background: var(--accent); vertical-align: middle; margin-left: 6px; min-width: 2px; opacity: 0.7; }
  .bar.body { background: var(--warn); }
  .cat { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
  .cat.user { background: rgba(210, 168, 255, 0.15); color: var(--user); }
  .cat.agent-sdk { background: rgba(121, 192, 255, 0.15); color: var(--agent); }
  .cat.plugin { background: rgba(255, 166, 87, 0.15); color: var(--plugin); }
  .cat.built-in { background: rgba(126, 231, 135, 0.15); color: var(--builtin); }
  .dup-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 700; background: rgba(248, 81, 73, 0.18); color: var(--hot); margin-left: 6px; vertical-align: middle; letter-spacing: 0.05em; }
  .detail { background: #0a0e13; padding: 14px 18px; font-size: 13px; color: var(--text); }
  .detail .path { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; color: var(--muted); margin-bottom: 8px; word-break: break-all; }
  .detail .full-desc { white-space: pre-wrap; line-height: 1.55; }
  .legend { font-size: 12px; color: var(--muted); margin-top: 14px; line-height: 1.7; }
  .legend code { background: var(--panel); padding: 1px 5px; border-radius: 3px; font-size: 11.5px; }
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
</style>
</head>
<body>
<header>
  <h1>Skill Chart</h1>
  <div class="sub">All Claude Code skills on this machine and the context they cost you.</div>
</header>
<main>

  <div class="stats" id="stats"></div>

  <details id="opt-panel" open>
    <summary>💡 Optimization suggestions</summary>
    <div id="opt-body"></div>
  </details>

  <div class="controls">
    <input type="search" id="q" placeholder="Search skills…" autofocus>
    <button class="pill active" data-cat="all">All</button>
    <button class="pill" data-cat="user">User</button>
    <button class="pill" data-cat="agent-sdk">Agent SDK</button>
    <button class="pill" data-cat="plugin">Plugin</button>
    <button class="pill" data-cat="built-in">Built-in</button>
    <button class="pill" data-cat="dup" title="Skills that exist in more than one location">⚠ Duplicates</button>
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th data-sort="name">Skill <span class="arrow">↕</span></th>
        <th data-sort="category">Source <span class="arrow">↕</span></th>
        <th data-sort="description">Description <span class="arrow">↕</span></th>
        <th data-sort="desc_tokens" class="num">Always-loaded<br><span style="font-weight:400;text-transform:none;font-size:10.5px;letter-spacing:0;">tokens (description)</span> <span class="arrow">↕</span></th>
        <th data-sort="body_tokens" class="num">When invoked<br><span style="font-weight:400;text-transform:none;font-size:10.5px;letter-spacing:0;">tokens (full SKILL.md)</span> <span class="arrow">↕</span></th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

  <div class="legend">
    <strong>How to read this:</strong> Token counts are estimated at ~4 chars per token (cl100k-style). The
    <em>always-loaded</em> column is the per-message cost — every skill's name + description is injected into your system
    prompt on every turn, whether you use the skill or not. The <em>when invoked</em> column is the additional cost the
    moment Claude calls <code>Skill</code> with that name. Built-in skills ship inside the Claude Code binary, so their
    bodies don't live on disk and aren't counted here.
  </div>

</main>

<script>
const SKILLS = __DATA__;

const tbody = document.getElementById("tbody");
const q = document.getElementById("q");
const statsEl = document.getElementById("stats");
const optEl = document.getElementById("opt-body");

let activeCat = "all";
let sortKey = "desc_tokens";
let sortDir = -1; // -1 desc, 1 asc

function fmt(n) { return n == null ? "—" : n.toLocaleString(); }

function maxBy(arr, k) { return arr.reduce((m, x) => Math.max(m, x[k] || 0), 0); }

function renderStats(rows) {
  const total = rows.length;
  const canonical = rows.filter(x => x.canonical);
  const alwaysLoaded = canonical.reduce((s, x) => s + (x.desc_tokens || 0), 0);
  const onDemandSum = canonical.reduce((s, x) => s + (x.body_tokens || 0), 0);
  const heaviest = canonical.reduce((m, x) => (x.body_tokens || 0) > (m?.body_tokens || 0) ? x : m, null);
  const dupes = rows.filter(x => x.duplicate && !x.canonical).length;
  statsEl.innerHTML = `
    <div class="stat"><div class="v">${canonical.length}</div><div class="l">Unique skills (loaded)</div></div>
    <div class="stat warn"><div class="v">~${fmt(alwaysLoaded)}</div><div class="l">Tokens always loaded</div></div>
    <div class="stat"><div class="v">~${fmt(onDemandSum)}</div><div class="l">Tokens if every skill fires</div></div>
    <div class="stat hot"><div class="v">${heaviest ? heaviest.name : "—"}</div><div class="l">Heaviest body (~${fmt(heaviest?.body_tokens)} tok)</div></div>
    ${dupes ? `<div class="stat hot"><div class="v">${dupes}</div><div class="l">Redundant disk copies</div></div>` : ""}
  `;
}

function render() {
  const term = q.value.trim().toLowerCase();
  let rows = SKILLS.filter(s => {
    if (activeCat === "dup") { if (!s.duplicate) return false; }
    else if (activeCat !== "all" && s.category !== activeCat) return false;
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

  renderStats(rows);
  renderOptimizations();

  const maxDesc = maxBy(rows, "desc_tokens") || 1;
  const maxBody = maxBy(rows, "body_tokens") || 1;

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">No skills match.</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((s, i) => {
    const descBar = Math.max(2, Math.round(((s.desc_tokens || 0) / maxDesc) * 80));
    const bodyBar = s.body_tokens != null ? Math.max(2, Math.round((s.body_tokens / maxBody) * 80)) : 0;
    return `
      <tr data-i="${i}">
        <td class="name">${escape(s.name)}${s.duplicate ? ' <span class="dup-badge" title="Also lives in another skill location — you may be paying for it twice">DUP</span>' : ''}</td>
        <td><span class="cat ${s.category}">${s.category}</span></td>
        <td class="desc" title="${escape(s.description)}">${escape(truncate(s.description, 140))}</td>
        <td class="num">${fmt(s.desc_tokens)}<span class="bar" style="width:${descBar}px"></span></td>
        <td class="num">${s.body_tokens == null ? '<span style="color:var(--muted)">built-in</span>' : `${fmt(s.body_tokens)}<span class="bar body" style="width:${bodyBar}px"></span>`}</td>
      </tr>
      <tr class="detail-row" data-i="${i}" style="display:none;">
        <td colspan="5" class="detail">
          <div class="path">${escape(s.path || s.source)}</div>
          <div class="full-desc">${escape(s.description)}</div>
        </td>
      </tr>
    `;
  }).join("");

  // expand/collapse on click
  tbody.querySelectorAll("tr[data-i]:not(.detail-row)").forEach(tr => {
    tr.addEventListener("click", () => {
      const i = tr.dataset.i;
      const detail = tbody.querySelector(`tr.detail-row[data-i="${i}"]`);
      const open = detail.style.display === "table-row";
      detail.style.display = open ? "none" : "table-row";
      tr.classList.toggle("expanded", !open);
    });
  });

  // header arrows
  document.querySelectorAll("th[data-sort]").forEach(th => {
    th.classList.toggle("sorted", th.dataset.sort === sortKey);
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.textContent = th.dataset.sort === sortKey ? (sortDir === -1 ? "↓" : "↑") : "↕";
  });
}

function renderOptimizations() {
  const canonical = SKILLS.filter(s => s.canonical);
  const editable = canonical.filter(s => s.category === "user" || s.category === "agent-sdk");
  const heavyDesc = [...editable].sort((a, b) => b.desc_tokens - a.desc_tokens).slice(0, 5);
  const dupes = SKILLS.filter(s => s.duplicate && !s.canonical);
  const TARGET_DESC = 50;
  const trimSavings = heavyDesc.reduce((sum, s) => sum + Math.max(0, s.desc_tokens - TARGET_DESC), 0);
  const dupSavings = dupes.reduce((sum, s) => sum + (s.body_tokens || 0), 0); // disk only — context is already deduped

  const sections = [];

  if (dupes.length) {
    sections.push(`
      <div class="opt-section">
        <h3>🗑️ ${dupes.length} redundant disk copies</h3>
        <p>These names exist as <strong>real directories</strong> in more than one location. Claude Code only reads <code>~/.claude/skills/</code>, so deleting the wrong copy will make the skill disappear from sessions. The safe fix: <code>rm -rf ~/.claude/skills/&lt;name&gt; && ln -s ~/.agents/skills/&lt;name&gt; ~/.claude/skills/&lt;name&gt;</code>. The dashboard auto-recognizes symlinks and stops flagging them.</p>
        <ul class="opt-list">
          ${dupes.map(s => `<li><code>${escape(s.name)}</code> — <span class="tok">${fmt(s.body_tokens)} body tok</span> wasted on disk</li>`).join("")}
        </ul>
      </div>
    `);
  }

  sections.push(`
    <div class="opt-section">
      <h3>✂️ Top 5 heaviest descriptions (always-loaded burden)</h3>
      <p>Every line below is injected into your system prompt on <em>every</em> turn. Trim each to ~${TARGET_DESC} tokens (the Anthropic skill-author guidance) and you save ~<strong>${fmt(trimSavings)} tokens per turn</strong>.</p>
      <ul class="opt-list">
        ${heavyDesc.map(s => `<li><code>${escape(s.name)}</code> — <span class="tok">${fmt(s.desc_tokens)} tok</span> · ${escape(truncate(s.description, 110))}</li>`).join("")}
      </ul>
      <span class="savings">Potential save: ~${fmt(trimSavings)} tok / turn</span>
    </div>
  `);

  const rare = canonical.filter(s => s.category === "user" || s.category === "agent-sdk")
                        .sort((a, b) => b.body_tokens - a.body_tokens)
                        .slice(0, 5);
  sections.push(`
    <div class="opt-section">
      <h3>🐘 Heaviest bodies (cost on invocation)</h3>
      <p>These don't load until invoked, but each call pulls a big chunk into context. Worth checking if you actually use them.</p>
      <ul class="opt-list">
        ${rare.map(s => `<li><code>${escape(s.name)}</code> — <span class="tok">${fmt(s.body_tokens)} tok</span></li>`).join("")}
      </ul>
    </div>
  `);

  optEl.innerHTML = sections.join("");
}

function escape(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function truncate(s, n) { s = s || ""; return s.length > n ? s.slice(0, n) + "…" : s; }

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


CATEGORY_PRIORITY = {"agent-sdk": 0, "user": 1, "plugin": 2, "built-in": 3}


def default_output_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "skillchart" / "dashboard.html"
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "skillchart" / "dashboard.html"


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


def main():
    ap = argparse.ArgumentParser(description="Audit Claude Code skills on this machine and emit an HTML dashboard.")
    ap.add_argument("-o", "--output", type=Path, help="Output path for the HTML dashboard. Defaults to a cache dir.")
    ap.add_argument("--json", action="store_true", help="Print the scanned skill data as JSON to stdout instead of writing HTML.")
    ap.add_argument("--no-open", action="store_true", help="Don't open the dashboard in the browser after building.")
    args = ap.parse_args()

    skills = discover_skills()
    # mark disk duplicates and pick a canonical entry per name (lowest priority wins)
    by_name: dict[str, list[dict]] = {}
    for s in skills:
        by_name.setdefault(s["name"], []).append(s)
    for name, entries in by_name.items():
        # Symlinks aren't duplicates — they're aliases pointing to the canonical version
        symlinked = [s for s in entries if s.get("symlink")]
        non_symlink = [s for s in entries if not s.get("symlink")]
        non_symlink.sort(key=lambda s: CATEGORY_PRIORITY.get(s["category"], 99))
        # Canonical = the user-category symlink (loaded by Claude Code) if present, else first non-symlink
        if symlinked:
            for s in symlinked:
                s["canonical"] = True
                s["duplicate"] = False  # treat the symlink and target as one
            for s in non_symlink:
                s["canonical"] = False
                s["duplicate"] = False
        else:
            for i, s in enumerate(non_symlink):
                s["duplicate"] = len(non_symlink) > 1
                s["canonical"] = i == 0
    skills.sort(key=lambda s: (-(s.get("body_tokens") or 0), s["name"]))

    if args.json:
        json.dump(skills, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    out_path = args.output or default_output_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out = HTML_TEMPLATE.replace("__DATA__", json.dumps(skills, ensure_ascii=False))
    out_path.write_text(out, encoding="utf-8")
    print(f"wrote {out_path}  ({len(skills)} skills)", file=sys.stderr)
    # quick text summary
    canonical = [s for s in skills if s["canonical"]]
    by_cat: dict[str, int] = {}
    total_desc = 0
    total_body = 0
    for s in canonical:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
        total_desc += s["desc_tokens"] or 0
        total_body += s["body_tokens"] or 0
    print(f"  unique loaded: {len(canonical)} skills, by category: {by_cat}", file=sys.stderr)
    print(f"  always-loaded ~{total_desc:,} tokens / on-demand sum ~{total_body:,} tokens", file=sys.stderr)

    if not args.no_open:
        open_in_browser(out_path)


if __name__ == "__main__":
    main()
