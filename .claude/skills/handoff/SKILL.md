---
name: handoff
description: Compact the current conversation into a handoff document for another agent to pick up.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Write a handoff document summarising the current conversation so a fresh agent can continue the work.

## Where it goes

Save it under `handoffs/` at the root of the repo you're working in, creating that directory if it doesn't exist. Outside a repo — or when the repo root isn't writable — use `handoffs/` under the current working directory instead, and say which one you used.

Name the file `YYYY-MM-DD-HHMM-<slug>.md`: the local time you write it, then a short kebab-case slug for the topic. The fixed-width timestamp prefix is the whole point — it makes a plain listing of the folder read in chronological order — so never abbreviate it, reorder it, or move it after the slug. Read the clock for it (`date +%Y-%m-%d-%H%M`); never write a timestamp from memory.

Open the file with the full timestamp and the working directory it was written from, so a reader holding only the file knows when and where it came from.

Leave the file uncommitted. Tell the user the path and let them decide whether it belongs in version control.

## What goes in it

Include a "suggested skills" section naming which skills the next agent should invoke.

Do not duplicate content already captured in other artifacts (specs, plans, ADRs, issues, commits, diffs). Reference them by path or URL instead.

Redact any sensitive information, such as API keys, passwords, or personally identifiable information.

If the user passed arguments, treat them as a description of what the next session will focus on and tailor the doc accordingly.
