---
name: skillchart
description: Audit every Claude Code skill on this machine and show its context cost. Scans ~/.claude/skills, ~/.agents/skills, plugin command caches, and built-in skills, then emits a self-contained dashboard.html with always-loaded vs when-invoked token estimates, duplicate detection, and concrete optimization suggestions. Use when the user asks "what skills do I have", "how much context do my skills cost", "find unused / heavy skills", "audit my skills", or invokes /skillchart.
---

# skillchart

Run the dashboard generator and open the result.

```bash
python3 /Users/davidbroza/skillchart/build.py && open /Users/davidbroza/skillchart/dashboard.html
```

That's it. The script is dependency-free; it reads SKILL.md frontmatter, totals tokens, flags duplicates, and writes `dashboard.html` next to itself.

If the user wants to act on the suggestions:

- **Resolve a duplicate:** Claude Code only reads `~/.claude/skills/` — deleting the copy there will make the skill disappear from sessions. After verifying the two copies are byte-identical (`diff -q`), replace the `~/.claude/` copy with a symlink to the SDK-managed one: `rm -rf ~/.claude/skills/<name> && ln -s ~/.agents/skills/<name> ~/.claude/skills/<name>`. One canonical source, both runtimes (CLI + Agent SDK) see it, SDK updates propagate.
- **Trim a heavy description:** edit the `description:` field in the skill's SKILL.md frontmatter. Aim for ~50 tokens (~200 chars). Keep the trigger phrases ("Use when the user says X") — those drive routing.
- **Archive an unused skill:** `mv ~/.claude/skills/<name> ~/.claude/skills.archive/<name>` so it stops loading but you can restore later.
