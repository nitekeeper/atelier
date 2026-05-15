# Self-Improvement Meeting — Cycle 10
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
Replacing superpowers with atelier requires closing enforcement gaps: `dev:tdd` and `dev:diagnose` lack Iron Law language that prevents rationalization. No lightweight `dev:verify` skill exists — `dev:qa` is a heavy pre-deploy phase gate, not a substitute.

## Agenda
1. What Iron Law language belongs in `dev:tdd` and `dev:diagnose`, and where exactly?
2. What is the minimal complete procedure for `dev:verify`?

## Discussion

### Agenda Item 1: Iron Law in `dev:tdd` and `dev:diagnose`

**Proposals:**
- Dr. Nadia Petrov: Rewrite Hard rules to name rationalization explicitly. Add missing rule to `dev:diagnose` about regression tests even on "obvious" bugs.
- Dr. Fatima Al-Rashid: Mandate must not carve out exceptions — exceptions go through bypass-confirm-log flow in `using-atelier`. Non-destructive.
- Dr. Yusuf Okafor: Exact text provided. Append after Hard rules section.
- Dr. Aisha Mensah: Placement *before* steps, not after. Post-hoc warnings are processed as caveats; pre-step framing sets interpretive context. Name specific rationalizations by phrase.
- Dr. Amara Osei-Bonsu: Iron Law works as a parsing anchor when paired with unconditional framing. Risk of habituation if overused — limit to these two skills.
- Dr. Yewande Diallo: Recommends soft-wall approach; the word "Iron" is ideologically loaded.

**Discussion:** Mensah's placement argument (before steps) won over Okafor's (after Hard rules) — pre-step framing is more effective. Diallo's soft-wall recommendation was discussed and dropped: the existing bypass-confirm-log flow in `using-atelier` already covers legitimate user exceptions, and absolute Hard rules already exist in other Atelier skills. Osei-Bonsu's habituation concern is addressed by limiting Iron Law to these two skills only.

**Decision:** Iron Law block before `## Procedure` in both skills, naming specific rationalizations — *Unanimous* (Diallo's soft-wall dropped)

### Agenda Item 2: `dev:verify` skill design

**Proposals:**
- Dr. Nadia Petrov: 5-step gate (identify → run fresh → read full output → verify → claim). No phase gate. Add to `using-atelier` guidance table.
- Dr. Fatima Al-Rashid: Hard rules require output to be quoted from actual tool output, not paraphrased from memory. Claim of success invalid if it precedes the tool call.
- Dr. Yusuf Okafor: Vacuity check as step 4 — temporarily break implementation, confirm test fails. High-value addition absent from superpowers' version.
- Dr. Aisha Mensah: Red flag framing as symptom-labeling: "this phrase is a known signal the gate was not completed" — not accusation.
- Dr. Amara Osei-Bonsu: Agents skip the "read" step — gate should require quoting output lines. Three additional failure rows: file written unverified, dependency installed unverified, subset-only suite run.
- Dr. Yewande Diallo: Neutral framing for red flags; avoids imputing bad faith.

**Discussion:** Vacuity check adopted (Okafor) — highest-value addition. Mensah and Diallo converged on symptom-labeling framing. Osei-Bonsu's three additional failure rows adopted. `skill_gates` DB entry dropped (no-gate skills need no row). `using-atelier` guidance update deferred to Cycle 13.

**Decision:** Create `dev:verify` with 5-step gate + vacuity check + Osei-Bonsu failure rows + symptom-labeling red flags — *Unanimous*

## Decisions Log
1. Add Iron Law block before `## Procedure` in `dev:tdd`, naming specific rationalizations — `skills/dev-tdd/SKILL.md`
2. Add Iron Law block before `## Procedure` in `dev:diagnose`, naming specific rationalizations — `skills/dev-diagnose/SKILL.md`
3. Create `skills/dev-verify/SKILL.md` with 5-step gate, vacuity check, expanded failure table — `skills/dev-verify/SKILL.md`

## Action Items
| # | Change | File(s) | Assigned to |
|---|---|---|---|
| 1 | Iron Law in dev:tdd | `skills/dev-tdd/SKILL.md` | Dr. Yusuf Okafor |
| 2 | Iron Law in dev:diagnose | `skills/dev-diagnose/SKILL.md` | Dr. Yusuf Okafor |
| 3 | Create dev:verify | `skills/dev-verify/SKILL.md` | Dr. Nadia Petrov / Dr. Yusuf Okafor |
