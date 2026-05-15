# Self-Improvement Meeting — Cycle 13
**Date:** 2026-05-15 UTC
**Facilitator:** Dr. Priya Nair (PM)
**Participants:**
| Agent | Role |
|---|---|
| Dr. Nadia Petrov | Agent Systems Architect |
| Dr. Fatima Al-Rashid | AI Safety Researcher |
| Dr. Yusuf Okafor | Prompt Engineer |
| Dr. Aisha Mensah | Cognitive Scientist |
| Dr. Yewande Diallo | AI Ethicist |
| Dr. Amara Osei-Bonsu | AI Research Scientist |

## PM Assessment
Upgrade `skills/using-atelier/SKILL.md` to match the forcing-function quality of `using-superpowers`: session-start mandate, instruction priority, SUBAGENT-STOP guard, mid-arc drift red flags, and integration of all new skills (dev:verify, dev:finish, dev:receive-review, dev:subagent, dev:write-skill) into the phase guidance table.

## Agenda
1. What forcing-function elements from using-superpowers should be adopted, adapted, or rejected for Atelier?
2. What structural changes (section order, table splits, new sections) maximize reliable following under implementation pressure?
3. How should the 5 new skills be integrated into the phase guidance table?

## Discussion

### Agenda Item 1: Forcing-function elements

**SUBAGENT-STOP guard:** All agents agreed the guard is needed. Petrov's refinement adopted: the guard mutes the trigger contract only — subagents dispatched by coordinators skip the ask gate but still apply the bypass procedure, since phase gates apply inside dispatched tasks. Detection signal: briefing header ("You are an implementer subagent" or similar). Al-Rashid proposed the guard also appear as a Red Flags row ("I'm a subagent working on a specific task, so using-atelier doesn't apply to me") — adopted.

**Priority hierarchy:** Diallo's "Authority and override" framing adopted in full. Key elements: live instruction override (explicit phrases recognized: "skip Atelier", "option (c)", "just do it"), persistent-preference satisfaction, and the clause that "skip Atelier" is option (c), not a conflict to bypass-log. Petrov contributed the Atelier-specific note distinguishing option (c) from bypass logging.

**EXTREMELY-IMPORTANT XML wrapper:** Rejected in favor of blockquote emphasis. Mensah's argument: `using-atelier` is injected as skill content in the assistant-turn context, not as a system prompt — XML tags carry less instruction-following weight in that context than in system-prompt context. Blockquote ritual framing (Mensah's "Session-open requirement") adopted instead.

**1% rule:** Adopted narrowly, scoped to the ask gate (rule 3 of the trigger contract) only. Petrov argued it should not extend to rules 1 and 2 (those are deterministic — phase gate and question/work-request distinction) because over-application would create false positives inside an active arc.

### Agenda Item 2: Structural changes

**Section reordering:** Bypass procedure promoted from position 7 (last) to position 4 (immediately after Trigger contract, before Red Flags). Mensah's argument: bypass procedure is the most operationally critical section and is currently in the lowest-attention position. Red Flags before phase guidance also adopted.

**Red Flags table split:** Al-Rashid's mitigation strategy adopted — split into two named subsections:
- "Trigger-firing red flags" (7 existing rows, unchanged)
- "Mid-arc drift red flags" (6 new rows)

This prevents table-length noise while expanding coverage. Mensah confirmed 7 existing rows have no semantic overlap and should not be consolidated.

**Session-open preamble:** Mensah's blockquote ("Session-open requirement") added after opening paragraph, before Priority hierarchy. Establishes ritual framing: verify Memex → identify active project and phase → select phase-recommended skill → respond.

### Agenda Item 3: New skills integration

Osei-Bonsu's cross-cutting structure adopted:
- `dev:subagent` added as alternative at `plan:approved` (parallel tasks)
- `dev:verify` added as preamble at `tdd:green` and `tdd:clean`; also added in cross-cutting section
- `review:changes-requested` row corrected from `dev:review` to `dev:receive-review` (existing row was a category error — `dev:review` is the reviewer's skill, not the implementer's response skill)
- `qa:approved` row updated to `dev:finish` (replacing `dev:handoff` which is a session-snapshot skill, not a closure mechanism)
- `handoff:open` row added (intermediate state created by dev:finish)
- Cross-cutting section added for `dev:verify` (general rule) and `dev:write-skill` (not phase-gated)

Dev arc diagram updated to show `dev:subagent` as alternative execution path entering at `plan:approved`, exiting at `tdd:clean`.

## Decisions Log
1. Add SUBAGENT-STOP guard before Trigger contract; mutes ask gate only — *Unanimous*
2. Add "Authority and override" priority hierarchy (user > methodology > defaults) — *Unanimous*
3. Add session-open preamble blockquote — *Unanimous*
4. Split Red Flags into trigger-firing (7 existing) + mid-arc drift (6 new) subsections — *Unanimous*
5. Promote Bypass procedure to position 4 (after Trigger contract) — *Unanimous*
6. Integrate 5 new skills into phase table; correct review:changes-requested category error — *Unanimous*

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Rewrite using-atelier SKILL.md with all 6 changes | `skills/using-atelier/SKILL.md` | All agents |
