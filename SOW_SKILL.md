---
name: sow-review-and-draft
version: 0.4.0
description: "Use this skill whenever a Statement of Work (SOW) needs to be reviewed or drafted for a Bristlecone client engagement. Trigger this for any request involving a draft SOW, a signed SOW, an SOW template, or design-discussion transcripts that should be turned into an SOW. Covers two modes: (1) REVIEW an existing/draft SOW against Bristlecone Delivery Excellence's actual audit checklist, with mandatory citations and confidence tags, plus a mandatory-section integrity pre-check that flags any standard-template section (termination, sign-off, opening statement and others) that has been removed or materially altered, and (2) GENERATE a first-draft SOW from a design-discussion transcript, short interview, or an already-generated BRD document, using Bristlecone's actual 22-section master SOW template. Always use this skill instead of giving a generic contract opinion — Bristlecone has specific section requirements and a specific template that this skill encodes."
compatibility: "Works standalone or inside the BRD tool. When the BRD tool has already produced an approved BRD document for a project, GENERATE mode can draft directly from it — pulling forward decisions the BRD already made instead of re-asking for them."
---

# SOW Review & Draft Generation

## 0. Read this section first — operating principles

These four rules apply to **every** check in this skill, in both modes.
They exist because the goal is not "sound confident" — it's "be
verifiably correct, and be honest about what isn't." A wrong-but-confident
finding is worse than no finding, because it wastes a reviewer's time
chasing a non-issue or, worse, gets trusted and misses a real one.

### Rule 1 — Cite, don't paraphrase-and-hope

Every finding **must** include the exact source text it's based on, quoted
verbatim, with a section/page/paragraph locator if the document has one.
No finding may be stated without a quote to back it. If you can't find a
quote to support a finding, you don't have a finding — you have a
suspicion, and it goes in "Open Questions" instead (see Rule 3).

Bad (no citation, unverifiable):
> Scope section is vague about integration work.

Good (citable, checkable by a human in 5 seconds):
> Section 4.6 says: *"Vendor will integrate with client's existing systems as
> needed."* — "as needed" has no defined boundary. Which systems, and who
> decides what's "needed," is not stated.

### Rule 2 — One check, one verdict, no bundling

Do not merge multiple checks into one vague verdict ("scope section has
some issues"). Every individual checkpoint listed in Section 2 of this
skill gets its own verdict with its own citation. Bundling hides exactly
which specific thing needs fixing and makes the report harder to act on —
the whole point of per-check granularity is that a delivery partner can go
fix the two things that failed without re-reading the whole section.

### Rule 3 — Never guess. Silence is not evidence of either state.

If the document is silent on something, that is **not** the same as it
being wrong, and it is **not** the same as it being fine. Silence means
"unknown," and unknown things go into the **Open Questions** list, not
into Pass or Fail.

- A section that explicitly states a bad term → **Fail**, with citation.
- A section that explicitly and adequately covers something → **Pass**,
  with citation.
- A section that says nothing on the topic at all → **Not Addressed**,
  listed separately, with a note on why it matters. Do not mark this Fail
  (that implies the document actively got it wrong) or Pass (that implies
  it's covered).
- A checkpoint that asks about something outside the document under
  review entirely (a CR log, a sign-off email, a Risk Register, an access
  log) → **Cannot Verify**, naming the companion evidence that would be
  needed. See Section 2's intro for when this applies broadly.

This distinction is the single biggest lever for precision in this skill.
Collapsing "wrong," "missing," and "out of scope for this review" into one
bucket is the most common way these reports become unreliable.

### Rule 4 — Confidence tag on every finding

Tag every finding **High / Medium / Low** confidence:

- **High** — the finding rests on an explicit, unambiguous quote. A human
  would reach the same conclusion reading the same sentence.
- **Medium** — the finding requires connecting two parts of the document
  (e.g., a cross-check between Scope and Exclusions) or a reasonable
  but not-certain reading of ambiguous wording.
- **Low** — the finding depends on domain/commercial judgment not fully
  verifiable from the text alone (e.g., "this margin looks thin" without
  knowing Bristlecone's actual cost base). Low-confidence findings are
  still worth surfacing, but must be phrased as a question or flag, not a
  verdict — see the report format in Section 3.

A report with High-confidence findings and Low-confidence questions kept
clearly separate is more useful — and more honest — than one with
everything stated with false uniformity.

### Rule 5 — Self-verification pass before output

After drafting the full report (Mode 1) or draft SOW (Mode 2), do a second
pass **before presenting it**, specifically checking:

1. Does every finding have a quote attached? (Rule 1) — if not, delete the
   finding or move it to Open Questions.
2. Does every quote actually say what the finding claims it says? Re-read
   the quote in isolation, as if you were a skeptical reviewer seeing it
   for the first time, and confirm it supports the stated verdict.
3. Are any two findings contradicting each other? (e.g., marking the same
   clause both a Scope pass and an Exclusions conflict without
   reconciling them)
4. Did "Not Addressed" and "Cannot Verify" get used correctly everywhere
   they apply, instead of Fail or a guessed Pass?
5. In GENERATE mode: does every placeholder use the exact bracket format
   in Section 4, so a human scanning the doc can find every open item
   with one search for `<`?
6. Mandatory sections (Section 2.0 registry) — in REVIEW mode, does every
   `Mandatory: true` row have a verdict, is every violation carrying the
   verbatim warning sentence and the Delivery Management escalation, and
   has every *uncertain* alteration call been demoted to an Open Question
   rather than flagged? In GENERATE mode, is every `Mandatory: true`
   section actually present in the draft, with `<CONFIRM: …>` rather than
   omission where content was unavailable?

Only after this pass is the output ready to show the user. If step 1-4
surface a problem, fix it and redo the pass — don't ship a report you
haven't re-checked.

---

## 1. Which mode?

- **REVIEW** — input is an existing draft or signed SOW. Default mode.
  Start here for any ambiguous request — Bristlecone has a backlog of
  already-signed SOWs intended to be run through Mode 1 first, to harden
  the checklist before Mode 2 is trusted.
- **GENERATE** — input is a design-discussion transcript, meeting notes, an
  already-approved BRD document from the BRD tool, or the user explicitly
  asks for a first-draft SOW.

If the request is ambiguous, ask which mode before proceeding — the two
produce very different outputs and guessing wrong wastes a full pass.

---

## 2. Mode 1: REVIEW — the checklist

This is Bristlecone Delivery Excellence's actual SOW Audit Checklist
(`SOW_AUDIT_CHECKLIST.pdf`), organized into the same 11 categories the
source document uses. It replaces the generic bootstrap checklist this
skill shipped with before the real one was available (see Changelog).

**Run Section 2.0 first.** It is a separate pre-check with its own verdict
set, covering whether the template's mandatory sections are still present
and intact. It is not one of the 101 checkpoints and is not scored with
them.

**Verdict set for every checkpoint in 2.1–2.11 below** — use exactly these
four, per Rule 3 (Section 2.0 has its own four; do not mix the two sets):

- **Pass** — the SOW (or supplied companion evidence) explicitly satisfies
  the checkpoint. Cite the quote.
- **Fail** — the SOW (or supplied evidence) explicitly contradicts or
  violates the checkpoint. Cite the quote.
- **Not Addressed** — the document is silent on the topic. Not a Fail.
- **Cannot Verify** — the checkpoint asks about something outside the
  document under review (a maintained CR log, a sign-off email, a Risk
  Register, an access-review log, an AI-usage tracker entry, a status
  report) and no such companion evidence was supplied. State plainly what
  evidence would be needed to verify it.

**Read this before scoring Categories 2–10:** most of their checkpoints
ask about **execution evidence** — a maintained log, a delivered report,
a signed email — not contract text. If the only input to this review is
the SOW document itself, expect most of these to land on **Cannot
Verify**. That is the correct, expected outcome, not a gap in the review.
Only mark **Fail** when the SOW's own text affirmatively contradicts the
checkpoint (e.g., it explicitly waives a control); only mark **Pass** when
the SOW commits to producing that artifact/process **and**, if
supplementary evidence was supplied alongside it, that evidence actually
shows it happened.

**Numeric precedents are examples, not thresholds to compare against.**
Several checkpoints below carry a specific number from the precedent audit
that originated them (e.g. GrowMark's 415-interface baseline, SPS
Commerce's $10/$20 shift rates, Google TMS's $7,826,000 cap, SPS's 2%
escalation from Jan 2024, Amgen's 20,160-hour/12-FTE staffing basis). When
applying the checkpoint's *pattern* to a different SOW, find and cite
**that SOW's own** stated number — never compare it against the precedent
engagement's figure unless you are literally reviewing that same
engagement.

### 2.0 Mandatory Section Integrity (pre-check — run before 2.1)

Some sections of Bristlecone's master SOW template exist to protect
Bristlecone, the client, or both, and are not negotiable away by deletion.
This pre-check answers one question per mandatory section: **is it still
there, and does it still do what it was put there to do?** It runs *before*
Categories 2.1–2.11 and is reported in its own block (Section 3), never
folded into the Pass / Fail / Not Addressed / Cannot Verify buckets — a
deleted termination clause is a categorically different event from a
checkpoint the document happens to be silent on, and burying it in a
101-row table is exactly how it gets missed.

The 101-checkpoint count in Categories 2.1–2.11 is unchanged; the checks
below are additional and separately reported.

#### The registry — mandatory flag for every template section

This table is the **single source of truth for both modes**. Mode 1 checks
against it; Mode 2 (Step 3) must emit every `Mandatory: true` row. Section
numbers are those of the 22-section master template in Section 4, Step 3.

| Template section | Mandatory | Confirmation status | Why it is / isn't mandatory |
|---|---|---|---|
| Cover page | true | Provisional | Identifies the contract instrument — client, contract ID, effective/end dates, version |
| 1. Agreement Details | true | Provisional | Opening statement binding this SOW to the governing MSA/ISA; without it the SOW has no legal parent |
| 2. Executive Summary | false | — | Narrative convenience; carries no protection of its own |
| 3. Project Overview | true | **Confirmed** | The 3.1 Background opening statement fixes the agreed current-state/business-driver premise the whole scope rests on |
| 4. Scope of Work | true | Provisional | 4.7 Exclusions is the primary scope-creep defence (see 2.2 Check 1) |
| 5. Proposed Architecture | false | — | Engagement-dependent; may legitimately not apply |
| 6. Solution Implementation Approach & Project Plan | false | — | Content matters in practice, but its structure varies legitimately by engagement type |
| 7. Project Management | false | — | Governance detail; the commercial protection sits elsewhere |
| 8. Personnel Requirements | false | — | Engagement-dependent |
| 9. Key Deliverables | true | Provisional | Nothing is invoiceable or acceptable without an enumerated deliverables list (2.3 Checks 1, 9) |
| 10. Key Assumptions | false | — | Strongly recommended, but its absence is a gap rather than a removed protection |
| 11. Acceptance Criteria | true | Provisional | The mechanism by which work is deemed complete and payable (2.3 Check 2) |
| 12. Obligations | true | Provisional | Client-side obligations are Bristlecone's only defence against client-caused delay |
| 13. Commercials | true | Provisional | Rates, cap, invoicing and payment terms — the commercial protection for both parties |
| 14. Performance Reporting & KPIs | false | — | 14.1 may be explicitly "Not Applicable" per the template's own instruction |
| 15. Risks | false | — | Recommended; not protective on its own |
| 16. Governance & Risk Mitigation | false | — | Cross-references protections that actually live in 9, 13 and 17 |
| 17. Change Management Process | true | Provisional | Without it, scope growth has no priced route (2.2 Checks 9, 11) |
| 18. Security & Data Protection | true | Provisional | Client-side regulatory protection, usually flowed down from the MSA |
| 19. Termination | true | **Confirmed** | Exit rights for both parties, including Termination for Cause and Effect of Termination |
| 20. Acceptance and Sign-Off | true | **Confirmed** | The signature/approval block — an unsigned SOW is unenforceable (2.1 Check 1) |
| 21. Key Observations | false | — | Self-descriptive commentary on the draft |
| 22. Reviewer Recommendations | false | — | Advisory |
| Appendix A — Technical Prerequisites | false | — | Supporting detail |
| Appendix B — Resource Profiles & Certifications | false | — | Supporting detail |

**Confirmation status is not decoration.** Rows marked **Confirmed** are the
three named in the originating requirement (opening statement, termination
clause, signature/approval block). Rows marked *Provisional* are this
skill's reasoned proposal and **must be ratified by the Delivery Management
team before this registry is treated as final**. Until they are, still run
the check on Provisional rows — just label them as provisional in the report
(Section 3 supplies the wording). Do not add or remove rows from this table
on your own judgment mid-review; if a review suggests the list is wrong, log
it per Section 5 instead.

#### Verdict set for this pre-check

Four verdicts, deliberately distinct from the 2.1–2.11 set:

- **Present — intact** — the section exists and still provides its
  protection. Cite the heading and one representative sentence.
- **Present — reworded (allowed)** — the text differs from the template but
  the protection is unchanged. **This is not a violation.** Note it in one
  line and move on.
- **Materially Altered** — **VIOLATION.** The section is still nominally
  present, but a protection it exists to provide is gone, reversed, or
  neutralised.
- **Removed** — **VIOLATION.** No section covering this substance exists
  anywhere in the document, under any heading or number.

Every violation is **High risk**, always, whichever section it hits.

#### Telling an allowed edit apart from a material alteration

This distinction is the whole precision problem of this check. Firing on
ordinary redlines trains reviewers to ignore the warning, which defeats it.

**Allowed — do NOT flag any of these:**

- Changing a number inside a retained clause: notice period 30 → 60 days,
  cure period, escalation percentage, cap value, rate figures.
- Party-specific substitution: names, addresses, entity forms, signatory
  names and titles, dates, governing-agreement reference.
- Rewording, tightening, translating into the client's house style, or
  splitting one paragraph into two.
- Reordering bullets or subsections within the section.
- Renumbering, or moving the section elsewhere in the document — **provided
  the substance still exists somewhere.** Search by substance, never by
  section number alone.
- Adding to the section: an extra termination ground, an extra security
  control, an extra client obligation, further regulatory references.
- Marking a subsection "Not Applicable" **where the template itself
  authorises it** (e.g. 14.1 SLAs on an implementation-only engagement).
- A `<CONFIRM: …>` / `<TECH TEAM: …>` / `<LEGAL: …>` placeholder standing in
  for unconfirmed content — the section is present and awaiting facts. That
  is a Mode 2 open item, not a violation.

**Material — DO flag these:**

- The section, or one of its numbered subsections, is absent entirely, or
  reduced to a heading with no operative text.
- A named protection inside it is deleted: 19.2 Termination for Cause gone,
  19.3 Effect of Termination gone, the Bristlecone column struck from the
  Section 20 sign-off table, 4.7 Exclusions gone, 12.2 Client Obligations
  gone.
- A right the template makes mutual is made one-sided, or is reassigned from
  one party to the other (e.g. termination for convenience available to the
  client only).
- An unqualified waiver, disclaimer or "notwithstanding" carve-out is added
  that negates the section's protection.
- The section is demoted from binding to advisory — "the parties will
  discuss termination in good faith" replacing an actual notice mechanism;
  change control described as optional; sign-off replaced by "deemed
  accepted" with no criteria.
- The section is merged into another in a way that drops substance. A merge
  that preserves every element is "Present — reworded (allowed)."

**Tie-break — when you cannot tell, do not flag.** Per Rule 3, an uncertain
material-alteration call goes to **Open Questions**, phrased as a question
("Section 19 no longer states any effect-of-termination consequence for
work-in-progress — was 19.3 intentionally dropped?"), not into the violation
block. A false violation costs a delivery partner an escalation to Delivery
Management over nothing, and teaches them to skip the warning next time.

**Explicitly out of scope for this check: *added* sections.** Non-standard
sections the parties have inserted — an extra annex, a client-specific
compliance schedule, a bespoke security addendum — are **not** a violation of
this control and must not be reported here. Assess their content under
Categories 2.1–2.11 like any other text.

#### Required warning format

Report every violation using exactly this block. The bolded sentence is
required verbatim — do not paraphrase it, soften it, or dissolve it into
surrounding prose:

```
### MANDATORY SECTION VIOLATION — <Section N. Name>
Type: Removed | Materially Altered
Risk: High · Confidence: <High/Medium/Low>
Registry status: Confirmed | Provisional — pending Delivery Management ratification

**This is a mandatory section from the Bristlecone standard SOW template
and cannot be removed.**

What changed: <one sentence — which protection is gone, reversed or neutralised>
Evidence: <verbatim quote of the altering text with its locator; or, for a
removal, the exact search performed and its result — e.g. "no heading or
body text matching 'terminat*', 'notice period' or 'effect of termination'
found anywhere in the document">
Action required: Consult the **Delivery Management team** before proceeding
with this removal or alteration.
```

Rule 1 applies unchanged: a violation with no quote and no stated search is
not a violation — it is an Open Question.

### 2.1 Agreement & Commercial Compliance (11 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | SOW signed by authorized representatives of both parties with full legal authority | All SOWs | High |
| 2 | Contract / SOW ID is documented and matches the purchase order | Google TMS | High |
| 3 | Effective date, end date, and any extension clauses are confirmed | All SOWs | High |
| 4 | Valid purchase order issued by client before services commenced | Google TMS | High |
| 5 | Invoicing schedule aligned to milestone/deliverable sign-offs (not just calendar dates) | GrowMark, Google TMS | High |
| 6 | Rate card correctly applied including role/band, location, and effective date | SPS Commerce | Medium |
| 7 | Annual rate escalation clause applied correctly (precedent: SPS Commerce, 2% from Jan 2024) | SPS Commerce | Medium |
| 8 | Shift allowance charges validated (precedent: SPS Commerce, $10 evening / $20 night) | SPS Commerce | Medium |
| 9 | Total invoiceable amount does not exceed the SOW cap (precedent: Google TMS, $7,826,000 max) | Google TMS | High |
| 10 | Travel & Expense claims are pre-approved, documented, and within policy | All SOWs | Medium |
| 11 | Carry-over vs. addition rate classification is correctly applied per hire date | SPS Commerce | Medium |

### 2.2 Scope Governance & Change Control (12 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | Scope inclusions and exclusions are clearly documented and agreed by both parties | All SOWs | High |
| 2 | All integrations are listed with a confirmed baseline count (precedent: GrowMark, 415 interfaces) | GrowMark | High |
| 3 | Custom code/object baseline documented (precedent: GrowMark, ~1,800 objects) | GrowMark | High |
| 4 | Any integration count exceeding the SOW baseline has a formal Change Request | GrowMark | High |
| 5 | Any custom object count exceeding the baseline has a formal Change Request | GrowMark | High |
| 6 | All deferred scope decisions are frozen before execution | GrowMark | High |
| 7 | Commercial and timeline impact of all deferred decisions is documented and signed off | GrowMark | High |
| 8 | Change Request log is maintained with tracking IDs, approval status, and evidence | Nvidia, GrowMark | High |
| 9 | All scope, timeline, and commercial changes followed the formal CR process (Appendix A) | Nvidia | High |
| 10 | Any delayed CR (precedent: GrowMark, 1.6-week delay) has been formally raised and logged | GrowMark | High |
| 11 | No work outside agreed scope has been performed without an approved CR | All SOWs | High |
| 12 | SOX audit activities and excluded services have not been performed under this SOW | Google TMS | Medium |

### 2.3 Deliverables & Acceptance Criteria (9 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | A detailed, explicit deliverables list exists (not just conceptual descriptions) | Nvidia, GrowMark | High |
| 2 | Each deliverable has objective, measurable acceptance criteria (not solely discretionary) | All SOWs | High |
| 3 | Acceptance criteria and payment milestones are linked per deliverable, not just per phase | GrowMark | High |
| 4 | Sign-off owners are identified for each deliverable | Nvidia | High |
| 5 | Formal sign-off evidence (email / document) exists for all completed deliverables | Nvidia | High |
| 6 | Key deliverables produced: project plans, OCM plans, data migration templates, status reports, config docs, test scripts, cutover plans | Google TMS | Medium |
| 7 | Post-release quality analysis reports (defect density, RCA, trend analysis) are produced after each release | Google TMS | Medium |
| 8 | Lessons Learned register is maintained and incorporated into future release plans | Google TMS | Low |
| 9 | The deliverables & acceptance-criteria section is fully populated, not left blank (precedent gap: SPS/Amgen) | SPS/Amgen | High |

### 2.4 Timeline & Project Plan (7 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | A detailed project plan with milestone dates exists (not just indicative timelines) | Nvidia | High |
| 2 | Staggered go-live releases are on track per the agreed phase schedule | Google TMS | High |
| 3 | Sprint planning, daily stand-ups, sprint reviews, and retrospectives are conducted regularly | Google TMS | Medium |
| 4 | All dependencies and blockers are identified, tracked, and escalated promptly | All SOWs | Medium |
| 5 | Timeline deviations are documented via Change Requests (not absorbed silently) | Nvidia | High |
| 6 | Project Risk Register is updated with new risks and mitigation strategies after each release | Google TMS | Medium |
| 7 | Agile metrics (velocity, burndown charts, cycle time) are tracked and reviewed | Google TMS | Low |

### 2.5 Resource & Staffing Compliance (9 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | Roles and responsibilities are clearly defined; a RACI matrix exists | Nvidia, SPS Commerce | High |
| 2 | All consultants meet the personnel qualification requirements in the SOW | SPS Commerce | High |
| 3 | Pyramid structure/headcount ratios are within the allowed deviations | SPS Commerce | Medium |
| 4 | Staffing capacity validated against scope volume (precedent: Amgen, 20,160 hrs ≈ 12 FTEs) | SPS/Amgen | Medium |
| 5 | On-site vs. offshore split is as agreed; location compliance confirmed | SPS Commerce | Medium |
| 6 | Program Manager is on-site as contractually required (precedent: SPS Commerce, Minneapolis) | SPS Commerce | High |
| 7 | Contractor personnel have been provisioned with client system access / badge access | Google TMS | Medium |
| 8 | Resource attrition and replacement follow the SOW process and are communicated to the client | All SOWs | Medium |
| 9 | Consultants are trained in Agile methodologies and tools (Jira, Scrum) | Google TMS | Low |

### 2.6 Quality Management & Testing (8 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | Test scripts and test cases are executed as planned for each sprint/release | Google TMS | High |
| 2 | Regression testing is conducted after each sprint or release | Google TMS | High |
| 3 | Automated testing tools are being utilized effectively | Google TMS | Medium |
| 4 | Defect density reports are generated and reviewed after each release | Google TMS | Medium |
| 5 | Root cause analysis conducted for all defects above threshold | Google TMS | Medium |
| 6 | All regulatory/compliance requirements (SOX, SDLC, ITCG) are being met | Google TMS | High |
| 7 | OCM plan using ADAPT/ADKAR methodology is in place and progress is tracked | Google TMS | Medium |
| 8 | Training materials and role mapping templates have been delivered per schedule | Google TMS | Medium |

### 2.7 Governance & Performance Reporting (9 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | Weekly status reports are produced and distributed on schedule | Google TMS, Nvidia | Medium |
| 2 | Monthly governance / QBR meetings are held with client and Bristlecone management | SPS/Amgen | Medium |
| 3 | KPIs and SLAs are defined, measured, and reported (precedent gap: absent in Nvidia SOW) | Nvidia | High |
| 4 | Performance reports include utilization, throughput, and cost per work unit | SPS Commerce | Medium |
| 5 | SLA grid is in text format, not embedded as an image; response/resolution targets confirmed | SPS/Amgen | High |
| 6 | P1–P3 priority tiers, response windows, and service credit/penalty terms are documented | SPS/Amgen | High |
| 7 | Support coverage windows (e.g. 24x7 for P1, business hours for P2/P3) are confirmed | SPS/Amgen | High |
| 8 | PMLC adherence is documented with a maintained reference (precedent: SPS/Amgen SharePoint) | SPS/Amgen | Medium |
| 9 | Reporting cadence dashboard and QBR content (MTTR, FTR, backlog health, RCA themes) are defined | SPS/Amgen | Medium |

### 2.8 Risk Management & Assumptions (7 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | A formal Risk Register exists and is actively maintained | Nvidia | High |
| 2 | Key assumptions are documented, monitored, and reviewed at each governance cycle | Nvidia | High |
| 3 | Deviation from assumptions triggers the Change Management process | Nvidia | High |
| 4 | Top risks have identified owners, probability/impact ratings, and mitigation plans | All SOWs | High |
| 5 | Vendor/third-party dependencies (e.g. Kinaxis, SAP RISE) have escalation paths and RACI | SPS/Amgen | Medium |
| 6 | Resource risk (attrition, availability, skill gaps) is tracked in the risk register | Google TMS | Medium |
| 7 | Integration and custom code volume risks are mitigated via unit-based caps or change control | GrowMark | High |

### 2.9 Security & Data Protection (10 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | All Bristlecone equipment has up-to-date OS patches and antivirus software | SPS Commerce | High |
| 2 | Client confidential data is encrypted in transit and at rest | SPS Commerce | High |
| 3 | Access to client systems follows the principle of least privilege | All SOWs | High |
| 4 | Segregation of Duties (SoD) controls are in place for admin activities | SPS/Amgen | High |
| 5 | Quarterly access reviews and access provisioning/de-provisioning logs are maintained | SPS/Amgen | High |
| 6 | Audit logging is enabled and evidence is retained per policy | SPS/Amgen | High |
| 7 | Data masking/anonymisation applied for non-production environments | SPS/Amgen | High |
| 8 | Information Protection Addendum compliance confirmed | Google TMS | High |
| 9 | Security incidents are reported to the client within the agreed timeframe | All SOWs | High |
| 10 | DR/BCP plan exists with confirmed RTO/RPO targets | SPS/Amgen | Medium |

### 2.10 AI Usage Governance (7 checkpoints)

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | AI Attachment compliance confirmed where AI is used in services or deliverables | Google TMS | High |
| 2 | AI tool usage is approved and aligned with the client's AI/GenAI policy | Google TMS, Amgen | High |
| 3 | No sensitive or confidential client data is submitted to public LLMs | All SOWs | High |
| 4 | Prompt governance logs and review records are maintained | SPS/Amgen | High |
| 5 | AI tool use is recorded in the Bristlecone AI in Delivery Tracker | Bristlecone Internal | Medium |
| 6 | AI-generated deliverables are reviewed and validated by qualified consultants before submission | All SOWs | High |
| 7 | Data residency and confidentiality boundaries for AI tools are defined and enforced | SPS/Amgen | High |

### 2.11 SOW-Specific Flags (12 checkpoints) — conditional category

**Apply this category only when the SOW under review is one of the five
named precedent engagements** (Google TMS, GrowMark S/4HANA, Nvidia IBP,
SPS Commerce, Amgen AMS). For any other SOW, mark all 12 items **Not
Applicable — different engagement** rather than forcing a verdict; do not
delete the rows from the report, since an explicit "Not Applicable" is
itself useful signal that this category was considered and correctly
skipped.

| # | Checkpoint | Precedent SOW | Risk |
|---|---|---|---|
| 1 | Deferred decisions (GTM vs Deal Capture, Event Management) are frozen and cost impact documented before execution proceeds | GrowMark | High |
| 2 | The 1.6-week delayed Change Request has been formally raised and logged | GrowMark | High |
| 3 | Integration scope increase beyond 415 interfaces has a signed CR | GrowMark | High |
| 4 | Supplementary artifacts exist outside the SOW: Project Plan, Deliverables Matrix, KPI dashboard, Risk Register, CR log, sign-off evidence | Nvidia | High |
| 5 | RACI matrix exists to operationalise the implicit roles in the SOW | Nvidia | High |
| 6 | Pyramid headcount ratios are within the allowed deviations; any changes are approved | SPS Commerce | Medium |
| 7 | Consultant hire dates are correctly classified as carry-over or addition rate | SPS Commerce | Medium |
| 8 | AI Attachment has been executed alongside the SOW | Google TMS | High |
| 9 | Bristlecone is tracking SAP TM release milestones against the $7,826,000 cap | Google TMS | High |
| 10 | The deliverables & acceptance-criteria section is fully populated with dated deliverables and sign-off owners | Amgen AMS | High |
| 11 | BOT (Build-Operate-Transfer) transition plan has phases, skill uplift targets, and dated completion criteria | Amgen AMS | Medium |
| 12 | Enhancement pipeline has a quarterly NTE (not-to-exceed) cap to prevent scope creep | Amgen AMS | High |

---

## 3. Mode 1 — Report format

Use this structure every time. Do not compress or skip the confidence tags
or citations to save space — precision is the entire value of this report.

```
# SOW Review Report — <Project/Client name>
Reviewed against: Bristlecone Delivery Excellence SOW Audit Checklist
(11 categories, 101 checkpoints) + the Section 2.0 mandatory-section
integrity pre-check
Date: <today's date>
Companion evidence available for this review (CR log, sign-off emails,
Risk Register, status reports, access logs, etc.): <list what was
supplied, or "None — SOW document only">

## Summary
<3-4 sentences: overall read, and the single biggest risk first. State
plainly whether this looks close to signable/compliant or needs
significant rework — do not hedge this into meaninglessness.>

## Mandatory Section Integrity (Section 2.0 pre-check)

<Report this block before the category findings, always — including when
nothing is wrong. If the registry has not yet been ratified by Delivery
Management, open with: "Registry rows marked Provisional below are this
skill's proposed list and are pending Delivery Management ratification;
Confirmed rows are settled.">

| Template section | Mandatory | Verdict | Confidence | Citation / search performed |
|---|---|---|---|---|
| Cover page | true | Present — intact / Present — reworded (allowed) / **Materially Altered** / **Removed** | H/M/L | "<quote>" (location), or the search run and its result |
| 1. Agreement Details | true | ... | | |
| 3. Project Overview | true | ... | | |
| 4. Scope of Work | true | ... | | |
| 9. Key Deliverables | true | ... | | |
| 11. Acceptance Criteria | true | ... | | |
| 12. Obligations | true | ... | | |
| 13. Commercials | true | ... | | |
| 17. Change Management Process | true | ... | | |
| 18. Security & Data Protection | true | ... | | |
| 19. Termination | true | ... | | |
| 20. Acceptance and Sign-Off | true | ... | | |

<Then, for every row whose verdict is Materially Altered or Removed, emit
the full warning block in the exact format given in Section 2.0. If there
are none, state: "No mandatory-section violations detected — all 12
mandatory template sections are present and substantively intact." Do not
omit this line; an explicit clean result is what tells the reader the check
actually ran.>

## Category-by-Category Findings

### 2.1 Agreement & Commercial Compliance
| # | Checkpoint | Verdict | Confidence | Citation |
|---|---|---|---|---|
| 1 | ... | Pass/Fail/Not Addressed/Cannot Verify | H/M/L | "<quote>" (location), or evidence needed |

[Repeat this table format for Categories 2.1 through 2.10. Include 2.11
only if the SOW under review is one of the five named precedent
engagements — otherwise state once: "2.11 SOW-Specific Flags: Not
Applicable — this SOW is not one of the five precedent engagements this
category was written against."]

## Audit Summary Scorecard
| Category | Total Items | Passed | Failed | Not Addressed | Cannot Verify | N/A |
|---|---|---|---|---|---|---|
| 1. Agreement & Commercial Compliance | 11 | | | | | |
| 2. Scope Governance & Change Control | 12 | | | | | |
| 3. Deliverables & Acceptance Criteria | 9 | | | | | |
| 4. Timeline & Project Plan | 7 | | | | | |
| 5. Resource & Staffing Compliance | 9 | | | | | |
| 6. Quality Management & Testing | 8 | | | | | |
| 7. Governance & Performance Reporting | 9 | | | | | |
| 8. Risk Management & Assumptions | 7 | | | | | |
| 9. Security & Data Protection | 10 | | | | | |
| 10. AI Usage Governance | 7 | | | | | |
| 11. SOW-Specific Flags | 12 | | | | | |
| **TOTAL** | **101** | | | | | |

(Mandatory-section violations are deliberately absent from this scorecard —
they are counted and reported only in the Mandatory Section Integrity block
above. Do not add them into any column here; a violation must not be able to
average away against 100 passing checkpoints.)

Overall Rating: **Red** if the Section 2.0 pre-check found any mandatory
section Removed or Materially Altered (this alone forces Red, whatever the
category tables say) · **Red** if any High-risk checkpoint is an outright
Fail (not Not Addressed/Cannot Verify), or multiple High-risk items are Not
Addressed with no mitigating explanation · **Amber** if there are
Medium/Low-risk Fails or Not-Addressed gaps but no High-risk Fails ·
**Green** if all High-risk checkpoints Pass (or are correctly N/A/Cannot
Verify with evidence explicitly requested) and no contradictions were
found. State the rating and the one-sentence reason for it.

## Top Risks (ranked, highest first)
1. <risk> — <why it matters commercially/legally> — <citation(s)>
2. ...
(Rank every Section 2.0 mandatory-section violation above everything else,
in registry order — a removed protection outranks a failed checkpoint about
how well a retained one is worded. Then surface any High-risk Fail
regardless of category, plus any missing Exclusions/Change-Management-process
finding — these are structurally high-risk per 2.2 and 2.3 above.)

## Contradictions Found
<any place two sections conflict — list both quotes side by side. If
none found, state "None found" rather than omitting the section.>

## Open Questions for the Team
<Low-confidence items and anything requiring commercial/legal judgment
this skill cannot make. Phrase as questions, not statements.>

## Cannot Verify — Evidence Needed
<Every checkpoint marked Cannot Verify, grouped by what companion document
would resolve it (e.g. "CR log — resolves checkpoints 2.2.8, 2.2.9" ), so
the requester knows exactly what to go fetch for a fuller review.>
```

---

## 4. Mode 2: GENERATE

### Step 1 — Extract what's explicitly stated

From the transcript/notes/design-discussion document/BRD document, extract
only what is **explicitly stated**, and attach a citation (timestamp/speaker
line for a transcript, heading/paragraph locator for a written document) to
each extracted fact, the same way Mode 1 requires citations. Do not infer
scope items that "seem implied" — if it wasn't said, it goes to Step 2.

A pre-engagement scoping document is the client's ask, not a negotiated
design — expect it to state objectives, evaluation criteria, and
constraints, but rarely final pricing, exact milestone dates, or named
signatories. Do not treat "nice-to-have"/evaluation-criteria language as
confirmed scope; extract it as stated, and let Step 2 surface what still
needs deciding before this becomes a committed SOW.

Never surface an internal artifact name, dashboard reference, or slide/page
locator from Bristlecone's own pre-sales process in the generated SOW
itself — the client-facing document should read as if authored fresh from
confirmed facts, not as a citation trail back to Bristlecone's internal
sourcing material. Citations/locators are for the human reviewer's benefit
during drafting (Step 1); they must not appear in the Step 3 draft output.

### Step 2 — Interview for gaps

Before drafting, ask the user for anything the source didn't cover,
batched into one list. Priority order (ask about these first — they're the
items most likely to derail a draft if guessed):
1. Which MSA/ISA/Consulting Agreement this falls under, and its date
2. Engagement type (Implementation / AMS / T&M / Support) and pricing model/figures
3. Exact milestone/phase dates and delivery location(s)
4. Named signatories/approvers on both sides
5. Out-of-scope items (transcripts almost never state these explicitly —
   assume this needs to be asked unless proven otherwise)
6. Any deferred scope decisions that must be frozen before kick-off

### Step 3 — Draft against Bristlecone's actual 22-section master template

This is Bristlecone's real master SOW template (`SOW_TEMPLATE.pdf`). Draft
every section below, in this order, using the exact table shapes shown.
Populate what Step 1/Step 2 confirmed; use the Step 4 placeholder
convention for everything else. **Do not use the template's own bare
`[Client Name]`-style brackets in your output** — those are the template's
own generic placeholders; this skill's `<CONFIRM: ...>` / `<TECH TEAM: ...>`
/ `<LEGAL: ...>` convention (Step 4) replaces them so a human can find every
open item with one search for `<`.

**Mandatory sections are not optional output.** Sections tagged
**[MANDATORY]** below are `Mandatory: true` in the Section 2.0 registry —
that table is the single source of truth, and any section not tagged here is
`Mandatory: false` there. Every mandatory section must appear in every
generated draft, in template order, **even when the transcript, interview or
BRD says nothing about it**. Silence in the source is never grounds for
dropping one: emit the section with its heading and subsections intact and
fill the unknown content with the Step 4 placeholder convention
(`<CONFIRM: …>` / `<TECH TEAM: …>` / `<LEGAL: …>`). A draft that omits a
mandatory section will be flagged as a violation the moment it is run
through Mode 1, so omitting it saves nothing. This is a floor, not a ceiling
— adding non-standard sections a specific engagement needs is fine and is
outside this control entirely.

**Table discipline** — only the sections explicitly called out below as a
table (the cover page field/details table, 4.6, 6.2, 7.2, 9, 11, 13.2, 14,
15, 17, 20) are tables. Every other section is prose/bullets, even if the
source material would tempt a tabular summary — a reviewer scanning the
document should encounter a table only where one is genuinely the clearest
way to show that data (rows of comparable items: integrations, phases,
roles, deliverables, rates, risks, sign-off lines), not as a generic
formatting habit. When in doubt, prefer prose.

**Cover page** **[MANDATORY]** — produce this *before* Section 1, as its own page. It is a
distinct page from Section 1 and uses a different field set — do not merge
the two. Title: **STATEMENT OF WORK**. Subtitle: the confirmed Project
Name (or `<CONFIRM: project name>` if not yet known) — never carry over
the blank template's own subtitle, *Master Template*, into a generated
draft. Below that, a Field/Details table with exactly these rows: Client
Name, Project Name, Engagement Type, Contract ID, Effective Date, End
Date, Document Version (start at v1.0), Prepared by (Bristlecone Inc.),
Review Status (Draft).

1. **Agreement Details** **[MANDATORY]** — one sentence: *"This Statement of Work (SOW) is
   entered into between [Client Name] and Bristlecone Incorporated under
   the [Master Services Agreement / Inbound Services Agreement] dated
   [Agreement Date]."* Then a **separate** Field/Details table — reusing
   the cover page's fields here is the single most common formatting
   mistake this step produces, so double-check before finalizing — with
   exactly these rows: Contract / SOW ID, SOW Effective Date, SOW End
   Date, Governing Agreement, Amendment / Supersedes, Agreement Duration,
   Delivery Location.
2. **Executive Summary** — one paragraph naming the engagement, three
   primary-objective bullets, and a closing sentence pointing to Section
   10 (Key Assumptions) and Section 21 (Key Observations) for deferred
   decisions. *(The template PDF's own Executive Summary text points to
   "Section 13" and "Section 16" for this — that's stale relative to the
   template's actual numbering, where Key Assumptions is Section 10 and
   Key Observations is Section 21. Use the correct section numbers, 10 and
   21, in generated output.)*
3. **Project Overview** **[MANDATORY]** — 3.1 Background (client's current state, pain
   points, business drivers); 3.2 Objectives (bullets); 3.3 Indicative
   To-Be Process Flow (high-level target-state description — if a relevant
   process-flow diagram is available from the source material, include it
   here rather than describing the flow in prose alone).
4. **Scope of Work** **[MANDATORY]** — 4.1 Geographical Scope; 4.2 Process Scope; 4.3
   Technical Scope; 4.4 Services Scope; 4.5 Application Scope; 4.6
   Integration Scope as a table (# | Integration | Source System | Target
   System | Notes); 4.7 Exclusions (bullets — this section is
   high-priority, see Mode 1 2.2 Check 1; never leave it a placeholder-only
   list if the source material gives any hint of what's out of scope).
5. **Proposed Architecture** — solution architecture, deployment model,
   change-governance structure. Include an architecture diagram here if one
   is available from the source material — a client and delivery team both
   read this section faster from a picture than from prose alone.
6. **Solution Implementation Approach & Project Plan** — 6.1
   Implementation Methodology (include a relevant implementation/rollout
   diagram if the source material has one); 6.2 Project Phases/Releases as
   a table (Phase/Release | Scope/Modules | Regions | Indicative Timeline |
   Status).
7. **Project Management** — 7.1 Governance Model; 7.2 Roles &
   Responsibilities as a table (Role | Party | Location |
   Responsibilities); 7.3 Work Schedule.
8. **Personnel Requirements** — bullets on language, background,
   domain familiarity, file-format/standards knowledge, methodology
   experience, certifications (reference Appendix B).
9. **Key Deliverables** **[MANDATORY]** — table (# | Deliverable | Description |
   Responsible Party | Due Date | Acceptance Criteria). Only list
   deliverables explicitly described in the source; mark unconfirmed dates
   `<CONFIRM: due date>` rather than "TBD" so it's search-findable.
10. **Key Assumptions** — bullets, project-specific (Rule: no generic
    boilerplate — see Mode 1 2.8-equivalent judgment). Note that deviations
    trigger the Change Management Process (Section 17).
11. **Acceptance Criteria** **[MANDATORY]** — table (Phase/Deliverable | Acceptance
    Criteria | Sign-off Party | Timeline for Review). Criteria must be
    objective/measurable — never "client satisfaction" with no test.
12. **Obligations** **[MANDATORY]** — 12.1 Bristlecone Obligations (bullets); 12.2 Client
    Obligations (bullets).
13. **Commercials** **[MANDATORY]** — 13.1 Engagement Model; 13.2 Rate Card & Pyramid
    Structure as a table (Role/Band | Location | Rate USD/day | Rate Type
    | Effective Date | Notes); 13.3 Shift Allowance; 13.4 Travel &
    Expenses; 13.5 Invoicing & Payment Terms.
14. **Performance Reporting & KPIs** — table (Report | Frequency |
    Recipients | Key Metrics); 14.1 SLAs & Targets (mark "Not Applicable"
    explicitly if this is an implementation-only engagement, per the
    template's own instruction — do not leave silent).
15. **Risks** — table (# | Risk Description | Likelihood | Impact |
    Mitigation). Start from the template's four standard risks (deferred
    scope decisions, custom code/integration overrun, key SME
    unavailability, non-objective acceptance criteria) and add any
    engagement-specific risk the source material surfaces.
16. **Governance & Risk Mitigation** — 16.1 Milestone Deliverables Matrix
    (reference Sections 9 and 13); 16.2 Deferred Scope Decisions (bullets);
    16.3 Integration & Custom Code Safeguards; 16.4 Change Control
    (reference Section 17).
17. **Change Management Process** **[MANDATORY]** — table (Step | Activity | Owner |
    Timeline), using the template's standard 6-step flow (CR raised → CR
    logged → impact assessment → CR reviewed/approved → SOW
    amendment/addendum → CR closure).
18. **Security & Data Protection** **[MANDATORY]** — bullets (patching/AV, encryption in
    transit/at rest, least privilege, information-security-policy
    compliance, incident reporting window, regulatory compliance e.g.
    GDPR/SOC2/ISO 27001).
19. **Termination** **[MANDATORY]** — 19.1 Termination for Convenience (notice period);
    19.2 Termination for Cause (breach/insolvency/fraud grounds); 19.3
    Effect of Termination.
20. **Acceptance and Sign-Off** **[MANDATORY]** — table (blank rows) with columns
    Bristlecone Inc. | [Client Name], rows Authorized Signatory / Name
    (Print) / Title / Date / Signature. Leave signature fields blank —
    never fabricate a name here.
21. **Key Observations** — factual, non-opinion bullets about *this
    draft's own content*: which critical scope decisions are deferred,
    whether dated deliverables were specified for major phases, the
    integration count and whether scope may expand, whether custom-code
    remediation estimates are assumptive, and the level (phase vs.
    deliverable) at which acceptance criteria/payment are linked. This
    section may be populated directly from the draft — it's self-descriptive,
    not a judgment call.
22. **Reviewer Recommendations** — 22.1 Governance & Risk Mitigation and
    22.2 Acceptance & Quality: standard recommendations in the template's
    style (request/confirm a Milestone Deliverables Matrix, freeze
    commercial impact of pre-project decisions, ensure acceptance criteria
    are objective, confirm change control covers all scope growth). For
    22.3 Overall Recommendation, **always** use
    `<CONFIRM: Proceed / Proceed with conditions / Do not proceed — pending
    a full Mode 1 REVIEW pass on this draft>` — a freshly generated draft
    should never rate its own signability.

**Appendix A — Technical Prerequisites** and **Appendix B — Resource
Profiles & Certifications**: include both, populated from the source where
possible, placeholders otherwise.

**Do not include Appendix C (Section Coverage Matrix)** in generated
drafts — it documents which historical precedent SOWs contributed which
template section during Delivery Excellence's own template-authoring
process. It's provenance metadata about the template itself, not part of
a deliverable SOW for a client engagement.

### Step 4 — Placeholder convention

Anything not confirmed in Step 1 or Step 2 gets a placeholder in this
exact format, so a human can find every open item with a single search
for `<`:

- `<CONFIRM: description of what's needed>` — for facts/decisions
- `<TECH TEAM: description>` — for technical detail outside this skill's
  competence
- `<LEGAL: description>` — for anything touching MSA-governed terms

Never fill these with a plausible-sounding guess instead of a placeholder.
A wrong guess in a contract is worse than an obvious blank.

### Step 5 — Mandatory self-review note

Every GENERATE-mode output ends with:

```
## Status of this draft
This is a first-pass draft. It has NOT been run through Mode 1 (REVIEW).
Before this goes to a client or gets a delivery-team sign-off, run it
through Mode 1 in full. Open placeholders in this draft: <count>.
All mandatory template sections (Section 2.0 registry) are present:
<Yes — all 12 / No — list which are missing and why>. Mandatory sections
carrying only placeholder content: <list, or "none">.
```

---

## 5. Improving this skill's accuracy over time

This skill's checklist and template should be versioned (see `version` in
the frontmatter) and updated based on real usage:

1. Every time a human reviewer says a finding was wrong, missed, or
   irrelevant, log it (project, category, checkpoint #, what should have
   happened).
2. Periodically fold confirmed patterns back into this file — either by
   tightening a checkpoint's guidance, adding a new one, or removing one
   that doesn't hold up. If Delivery Excellence revises the audit
   checklist or the master template, re-sync Sections 2 and 4 against the
   new source documents rather than patching around drift. The Section 2.0
   mandatory registry is part of that re-sync: log every case where a
   reviewer says a section was flagged that shouldn't have been (or wasn't
   flagged and should have been), and take the registry's Provisional rows
   back to Delivery Management for ratification or removal — the flag is a
   contractual judgment, so it changes only with their sign-off, never on a
   reviewer's or this skill's own initiative mid-review.
3. Bump the `version` field and keep a one-line changelog at the bottom of
   this file so it's clear what changed between runs.

This is the mechanism by which the skill gets *more* accurate over
successive real SOWs, rather than a claim of accuracy up front that
hasn't been tested against Bristlecone's actual documents yet.

## Changelog

- v0.4.0 — Added a mandatory-section guardrail so the protections the master
  template exists to carry cannot be quietly deleted from a SOW. New Section
  2.0 holds the registry — an explicit `Mandatory: true/false` flag for all
  24 template sections (cover page, 22 numbered sections, 2 appendices) —
  and runs as a REVIEW pre-check ahead of Categories 2.1–2.11, with its own
  four-verdict set (Present — intact / Present — reworded (allowed) /
  Materially Altered / Removed) so a deleted clause is never diluted into
  the generic Pass / Fail / Not Addressed / Cannot Verify buckets. Every
  violation is High risk, carries the verbatim warning *"This is a mandatory
  section from the Bristlecone standard SOW template and cannot be removed"*
  and an instruction to consult the Delivery Management team, is reported in
  its own block in Section 3, forces an Overall Rating of Red, and outranks
  every other Top Risk. To keep the check from crying wolf, 2.0 lists the
  edits that are explicitly *allowed* (notice-period and other in-clause
  numbers, party-specific detail, rewording, reordering, renumbering or
  relocation where the substance survives, additions, template-authorised
  "Not Applicable" markings, open `<CONFIRM: …>` placeholders) against those
  that are material (section or subsection gone or emptied, a named
  protection deleted, a mutual right made one-sided, a negating waiver
  added, a binding clause demoted to advisory, a lossy merge) — and requires
  an uncertain call to be demoted to an Open Question per Rule 3 rather than
  flagged. *Added* non-standard sections are explicitly out of scope for
  this control. Mode 2 Step 3 now tags each mandatory section
  **[MANDATORY]** inline and requires all of them in every draft even when
  the source material is silent, using the Step 4 placeholder convention
  instead of omission; Step 5's status note reports mandatory-section
  coverage, and Rule 5 gains a step 6 covering both modes. **Registry status:
  Project Overview (3), Termination (19) and Acceptance & Sign-Off (20) are
  Confirmed; the other nine `true` rows are marked Provisional and need
  Delivery Management ratification before the list is treated as final.**
  Note the section-numbering translation this required: the originating
  requirement was written against v0.1.0's generic 13-section checklist
  ("termination 2.11", "signatures 2.13"), which v0.2.0 replaced — the
  mandatory sections live in the real 22-section master template in Section
  4, so Termination is 19 and Sign-Off is 20, not 2.11 and 2.13.
- v0.3.0 — Many client engagements have no RFP/pre-sales dashboard of their
  own, so generated SOWs must never carry Bristlecone-internal sourcing
  references (RFP framing, slide/page locators back to an internal deck)
  into client-facing output — Step 1 now uses source-neutral language and
  explicitly forbids internal locators leaking into the Step 3 draft. Added
  an explicit table-discipline rule to Step 3 (tables only in the sections
  that already specified one) to stop tables creeping into prose sections.
  Added guidance to include available process-flow/architecture/
  implementation diagrams in Sections 3.3, 5, and 6 rather than describing
  them in prose only.
- v0.2.1 — Fixed a Step 3 drafting bug found against a real generated draft:
  the old instructions folded the template's own **cover page** (Title +
  Client Name/Project Name/Engagement Type/Contract ID/Effective Date/End
  Date/Document Version/Prepared by/Review Status table) into "Section 1.
  Agreement Details," so generated drafts had no cover page at all and
  Section 1 used the wrong field set. `SOW_TEMPLATE.pdf`'s actual Section 1
  has its own distinct table (Contract/SOW ID, SOW Effective Date, SOW End
  Date, Governing Agreement, Amendment/Supersedes, Agreement Duration,
  Delivery Location) plus an opening sentence naming the governing
  agreement. Step 3 now instructs both, as separate pages/tables.
- v0.2.0 — Replaced the generic bootstrap 13-section REVIEW checklist with
  Bristlecone Delivery Excellence's actual SOW Audit Checklist
  (`SOW_AUDIT_CHECKLIST.pdf`): 11 categories, 101 checkpoints (the source
  document's own cover page and scorecard both label the total "93" —
  that label doesn't reconcile with its own per-category counts, which sum
  to 101; this skill uses the counted total). Replaced the generic
  13-section GENERATE draft structure with Bristlecone's actual 22-section
  + 2-appendix master SOW template (`SOW_TEMPLATE.pdf`); Appendix C
  (Section Coverage Matrix) intentionally excluded from generated output
  as template provenance metadata, not deliverable content. Corrected a
  stale internal cross-reference in the source template (Executive
  Summary pointed to "Section 13/16" for Key Assumptions/Key Observations;
  actual numbering is Section 10/21).
- v0.1.0 — Initial version. Generic 13-section checklist (not yet mapped
  to Bristlecone's actual template). Citation, confidence-tagging, and
  self-verification rules established.
