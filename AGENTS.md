# AGENTS.md

## Agent skills

This repo carries its own agent skills in `.claude/skills/`, committed so they travel with
the clone — no per-machine install, and the same set on every machine.

Each skill is a folder holding a `SKILL.md` plus an `agents/openai.yaml`. Claude Code reads
the `SKILL.md` frontmatter and discovers `.claude/skills/` automatically. Codex reads the
YAML; point it at the path below when you need one.

Where a skill says **invoke the `X` skill**, read that as: in Claude Code, call the Skill
tool with `X`; in Codex, invoke the skill of that name; on a harness with neither, open
that skill's `SKILL.md` and follow it inline.

### Model-invoked

Reachable by the agent on its own, or by name.

| Skill | Path | What it's for |
| --- | --- | --- |
| `unslop` | `.claude/skills/unslop/` | Cut AI tells from any writing. Always applies. |
| `tdd` | `.claude/skills/tdd/` | Red-green-refactor loop, one vertical slice at a time. |
| `diagnosing-bugs` | `.claude/skills/diagnosing-bugs/` | Diagnosis loop for hard bugs and perf regressions. |
| `prototype` | `.claude/skills/prototype/` | Throwaway prototype to answer a design question. |
| `research` | `.claude/skills/research/` | Investigate against primary sources, leave a cited Markdown file. |
| `grilling` | `.claude/skills/grilling/` | Interview the user until every branch of the design tree is resolved. |
| `codebase-design` | `.claude/skills/codebase-design/` | Vocabulary for deep modules — `tdd` leans on this. |
| `domain-modeling` | `.claude/skills/domain-modeling/` | Build and sharpen the domain model; `CONTEXT.md` and ADRs. |

### User-invoked

Only fire when the human types them — they carry no description the model can see.

| Skill | Path | What it's for |
| --- | --- | --- |
| `grill-me` | `.claude/skills/grill-me/` | Get relentlessly interviewed about a plan or design. |
| `handoff` | `.claude/skills/handoff/` | Compact the conversation into a handoff doc under `handoffs/`. |

### Handoffs

`handoff` writes to `handoffs/` at the repo root, named `YYYY-MM-DD-HHMM-<slug>.md`, so a
plain listing of that folder reads in chronological order. The files are left uncommitted —
committing one is a deliberate choice.

source: adapted from https://github.com/mattpocock/skills (commit 9c9f36c) via NickMarcha/agents.md
