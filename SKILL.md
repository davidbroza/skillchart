---
name: skillchart
description: Audit every Claude Code skill on this machine and show its context cost. Scans ~/.claude/skills, ~/.agents/skills, plugin command caches, and built-in skills, then emits a self-contained dashboard.html with always-loaded vs when-invoked token estimates, invocation counts from session logs, duplicate detection, and plan-aware budget visualization. Use when the user asks "what skills do I have", "how much context do my skills cost", "find unused / heavy skills", "audit my skills", or invokes /skillchart.
---

# skillchart

Run the dashboard generator and open the result.

```bash
skillchart
```

That's it. The CLI builds an HTML dashboard at `~/Library/Caches/skillchart/dashboard.html` and opens it in the default browser.

## Common flags

```bash
skillchart --json                # machine-readable output
skillchart --fix-dupes           # show byte-identical disk dupes (dry run)
skillchart --fix-dupes --apply   # actually replace duplicates with symlinks
skillchart --no-usage            # skip session-log parsing (faster)
skillchart --plan max-20x        # one-off plan override for the budget panel
skillchart --set-plan max-5x     # persist the plan to ~/.config/skillchart/config.json
skillchart --show-config         # print resolved config
```

If `skillchart` isn't on PATH, fall back to `python3 ~/skillchart/build.py` (or wherever the repo lives) — same behavior. The script is dependency-free; `tiktoken` is optional and gives ~10× more accurate token counts when present.

## Acting on the dashboard's suggestions

- **Resolve a duplicate:** Claude Code only reads `~/.claude/skills/` — deleting the copy there will make the skill disappear from sessions. After verifying the two copies are byte-identical (`diff -q`), replace the `~/.claude/` copy with a symlink to the SDK-managed one: `rm -rf ~/.claude/skills/<name> && ln -s ~/.agents/skills/<name> ~/.claude/skills/<name>`. One canonical source, both runtimes (CLI + Agent SDK) see it, SDK updates propagate. Or just run `skillchart --fix-dupes --apply`.
- **Trim a heavy description:** edit the `description:` field in the skill's SKILL.md frontmatter. Aim for ~50 tokens (~200 chars). Keep the trigger phrases ("Use when the user says X") — those drive routing.
- **Archive an unused skill:** `mv ~/.claude/skills/<name> ~/.claude/skills.archive/<name>` so it stops loading but you can restore later.

## Repo

Source + brew install + issue tracker: https://github.com/davidbroza/skillchart
