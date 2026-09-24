<!-- Generated from Pitch Checklist_v1.3.xlsx. Hand-maintained thereafter. -->

# Pitch Deck Scorecard

The quality rubric applied to an RFP/proposal deck **before** a SOW is drafted from
it. Source: Bristlecone S&C Sales Enablement Pitch Toolkit v1.3, tabs
`Pitch Checklist` and `Summary and Scoring`.

This file is the runtime source of truth. `app/pitch_scorecard.py` parses it — the
category headings and the criteria tables are a machine interface, so keep their
shape when editing. Criterion ids are `<category number>.<row number>` (`12.1`) and
are **stable identifiers**: renumbering a row re-points every saved answer that
referenced it.

The score is **advisory**. It never blocks SOW creation. Its job is to tell an author
what to fix while there is still time to fix it.

---

## 0. Operating rules — read these before rating anything

### Rule 1 — Every rating cites a slide, or says it found nothing
A rating with no citation is an opinion, not a review. Name the slide you are relying
on (`slide 87`, `slides 91-92`). If the criterion's subject is absent from the deck,
say so explicitly — "no RACI matrix anywhere in the deck" — rather than leaving the
comment vague. Never cite a slide number you have not actually seen in the material
given to you.

### Rule 2 — "Absent" and "unassessable" are different findings
Both may score `Does Not Meet`, but they tell the author to do different things:
- **Absent** — the content is not in the deck. "No client quotes or testimonials anywhere."
- **Unassessable** — the content exists but cannot be judged. "Every cost line in the
  Commercials table (slide 87) is a literal 'xxx' placeholder, so there is no basis of
  estimate to assess."

Set `evidence_status` to `"found"`, `"absent"` or `"unassessable"` accordingly, and make
the comment say which.

### Rule 3 — Not Applicable is a verdict, not an escape hatch
`Not Applicable` means the criterion cannot apply to this deal — a partnership criterion
on a deal with no partner. It removes the criterion from the category average. It does
**not** mean "the deck does not cover this" (that is `Does Not Meet`) and it is not a way
to avoid a judgement you find difficult. Only the categories flagged *may be N/A* in
section 2 are candidates for wholesale exclusion.

### Rule 4 — No credit for content you cannot see
Do not award a rating on the assumption that something sits in an appendix, a linked
document, or a later version. Score the deck you were given. If the deck references an
attachment you do not have, that is worth a comment, not a score.

### Rule 5 — One criterion, one verdict, no bundling
Each criterion gets its own rating and its own comment. Do not write "see above", do not
give two criteria one shared answer, and do not let a strong sibling criterion lift a weak
one. Adjacent criteria in a category often look similar — read their anchors carefully and
score the distinction the rubric is actually drawing.

### Rule 6 — Self-verification pass before you answer
Before emitting, re-read your own output and check:
1. Every criterion in the category has exactly one rating from the legal set.
2. Every cited slide number exists in the deck you were shown.
3. No comment contradicts another comment in the same category.
4. Each rating is the one its anchor text actually describes — not one notch kinder.

### Rule 7 — The score is advisory, so make it actionable
A gap the author cannot act on is wasted words. Every criterion scoring below Best
Practice carries a `to_improve` line naming the concrete change: which slide to add,
what number to populate, which section is missing. "Strengthen the narrative" is not
actionable. "Add a closing 'Why Bristlecone' section; the deck currently ends on the
commercials table" is.

---

## 1. Rating scale and scoring math

| Rating | Score | Meaning |
|---|---|---|
| `Best Practice` | 2 | Meets the best-practice anchor |
| `Minimally Meets` | 1 | Present but falls short of the anchor |
| `Does Not Meet` | 0 | Absent, or present but unassessable |
| `Not Applicable` | — | Excluded from the average (see Rule 3) |

Roll-up, per category:

```
category_avg    = mean of the scores of its non-N/A criteria
category_score% = category_avg / 2
category_weight = the weight declared on the category heading
weighted%       = category_score% * category_weight
TOTAL           = sum of weighted% across all 14 categories
```

Readiness bands on TOTAL:

| Band | Meaning |
|---|---|
| ≥ 80% | **SUBMISSION-READY** |
| 60–79% | **NEEDS WORK** — address High/Critical gaps |
| > 0% | **NOT READY** — major rework required |

If an entire category is `Not Applicable`, its weight becomes 0 and the remaining
weights are renormalized to total 1.00, so the percentage stays comparable across deals.

---

## 2. The rubric — 14 categories, 57 criteria, weights total 1.00

The three anchor columns **are** the rating scale. Read them as written; do not
paraphrase them into a different standard.


### 1. STRUCTURE (4 criteria, weight 0.07)

**What this category covers:**

The deck includes all essential sections to ensure a compelling, well-structured proposal:

- Executive Summary
- Our Understanding (Client’s Challenges and Needs)
- Bristlecone’s Point of View (PoV)
- Proposed Solution (Approach, Methodology, Key Components)
- Business Case / Value Delivered (Expected Impact, ROI)
- Examples & Case Studies (Relevant Success Stories)
- Accelerators & Differentiators (Proprietary Tools, Frameworks)
- Value Proposition (Pricing, Investment Details)
- Why Bristlecone? (Key Reasons We’re the Best Partner)
- Additional Value-Added Services (Recommendations Beyond the Ask that may be Differentiators)

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | CLIENT-SPECIFIC RELEVANCE | Generic content that could apply to any client. | Some customization, but still largely templated. | Fully tailored to the client, incorporating industry, domain, and solution-specific details. |
| 2 | THOUGHT LEADERSHIP | No additional recommendations beyond the RFP scope. | Some extra insights, but not well-developed. | Provides a strong Bristlecone PoV, proactively recommending relevant services based on expertise. |
| 3 | EXECUTIVE SUMMARY EFFECTIVENESS | No executive summary, or it does not stand alone. | Summary present but descriptive rather than persuasive; buries the value. | Compelling, stand-alone exec summary leading with client value, outcomes, and why Bristlecone. |
| 4 | NARRATIVE FLOW & COHERENCE | Sections feel disconnected; no logical storyline. | Logical order but transitions/threading of win themes are weak. | Sections build a coherent, client-centered storyline from problem to value. |

### 2. CUSTOMER CENTRICITY (3 criteria, weight 0.10)

**What this category covers:**

The proposal demonstrates a deep understanding of the client’s strategy, current situation, and specific requirements by:

- Clearly connecting the proposed solution to the client’s strategic priorities and business objectives.
- Framing the narrative from the client’s perspective, not just Bristlecone’s.
- Grounding recommendations in industry and domain-specific context to enhance credibility.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | CLIENT-SPECIFIC INSIGHTS | Generic content with little to no client or industry specificity. | Largely standardized deck with minimal tailoring (e.g., just rewording the client’s RFP). | Goes beyond restating client needs – demonstrates strategic understanding of their business, industry, and challenges. |
| 2 | SOLUTION ALIGNMENT | Weak or no connection between the solution and the client’s strategic priorities. | Some linkage, but lacks depth in explaining how Bristlecone’s approach uniquely addresses the client’s needs. | Solution is clearly mapped to the client’s goals, with strong rationale and supporting details. |
| 3 | COMPELLING STORYTELLING | Proposal is written from Bristlecone’s perspective, not the client’s. | Some elements of client perspective but not consistently applied. | The story is structured around the client’s challenges and goals, making them the hero of the narrative. |

### 3. THOUGHT LEADERSHIP & EXPERTISE (4 criteria, weight 0.07)

**What this category covers:**

The proposal effectively showcases Bristlecone’s expertise and credibility across all relevant capabilities required for the scope of work, including:

Industry Knowledge: Deep understanding of sector-specific challenges and opportunities.
Technology Knowledge: Expertise in relevant package applications and partner technologies.
AI Knowledge: Demonstrated understanding of AI-driven solutions and their applications (if applicable).

Domain Knowledge: Specialization in key areas such as Planning, Procurement, Operations, and Manufacturing.
Program Management: Proven methodologies for project implementation and execution.
Change Management: Ability to drive adoption, transformation, and impact.

Additionally, the proposal should include a Bristlecone Point of View (PoV) on:

- Industry Trends and Challenges
- AI Opportunities and Innovation
- The Evolving SCM Landscape

Where applicable, thought leadership should be reinforced with data, case studies, and public citations to build credibility.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | EXPERTISE COVERAGE | Key capabilities missing or weakly represented. | Lists expertise areas but lacks depth or compelling examples. | Fully covers all expertise areas with detailed supporting content. |
| 2 | PROOF & THOUGHT LEADERSHIP | Relies on generic statements without proof points. | Some examples included, but lacks depth, maturity, or industry citations. | Uses strong case studies, public citations, and thought leadership insights to reinforce credibility. |
| 3 | POINT OF VIEW (POV) | No Bristlecone PoV or generic industry statements. | PoV included but lacks specificity or unique perspective. | Strong, well-defined Bristlecone PoV with insights, recommendations, and supporting data. |
| 4 | DEMONSTRATIONS & EVIDENCE | No demos or tangible proof where relevant. | Some evidence included, but not fully developed. | Where applicable, includes live demos, interactive elements, or detailed use cases. |

### 4. PARTNERSHIPS (4 criteria, weight 0.10) — category may be N/A

**What this category covers:**

For deals involving technology or service partnerships, the proposal should clearly articulate:

- The breadth and depth of the partnership.
- The value of the partnership to the success of the program.
- Examples of past successful partnerships, aligned to the deal.
- If the partnership is new or early-stage, the proposal must define a clear path to successful delivery.
- Joint case studies should be included, where appropriate, to demonstrate proven collaboration and execution.

If no external partnerships are relevant to the proposal, this section is marked N/A.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | PARTNERSHIP VALUE DEFINITION | Standard, generic content with no tie to the deal. | Includes partnership details, but lacks strong alignment to this specific opportunity. | Clearly defines how the partnership enhances program success with tailored insights. |
| 2 | PROOF OF SUCCESS | No examples or only vague references to past success. | Some success stories included, but lacks depth or relevance. | Robust, well-aligned case studies showcasing joint success with partners. |
| 3 | PATH TO SUCCESS FOR NEW RELATIONSHIPS | No roadmap for execution with an early-stage partner. | Some consideration given, but lacks details on collaboration and risk mitigation. | Well-defined success path, including roles, responsibilities, and joint execution strategies. |
| 4 | COLLABORATION FACTORS | No discussion of how Bristlecone, the partner, and the client will collaborate. | Some reference to collaboration, but not fully developed. | Clearly defines critical success factors for seamless partnership execution in this specific deal. |

**Assessing this category.** If the engagement genuinely involves no external technology or service partner, rate all four criteria `Not Applicable` — that is the rubric's own instruction, and it excludes them from the average rather than scoring them zero. A partner named anywhere in the deck (SAP, a hyperscaler, an ISV) means the category applies.

### 5. TEAM (5 criteria, weight 0.06)

**What this category covers:**

The proposal should clearly define the Day 1 / Delivery Team, ensuring:

- Key roles and responsibilities are identified.
- Bios are included for all key personnel, tailored to highlight their fit for the proposed role and relevance to the client’s needs.
- Client participation is well-defined, outlining expected roles and effort required at each key phase of the program.
- A RACI matrix (Responsible, Accountable, Consulted, Informed) is included for key deliverables, ensuring clarity in ownership and execution.
- FTE requirements by phase are specified at an actionable level (weekly/monthly) where applicable.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | KEY PERSONNEL & ROLES | Key roles are missing or loosely defined. | Full list of key roles included but lacks strong contextualization. | Key roles fully defined with clear alignment to the deal. |
| 2 | BIOS & FIT FOR ROLE | Generic bios with no relevance to this engagement. | Bios included but not tailored to emphasize fit. | Bios are client-specific, highlighting direct relevance to the project. |
| 3 | CLIENT PARTICIPATION DEFINITION | No mention of client engagement expectations. | Some client requirements outlined, but not detailed. | Client roles, responsibilities, and effort levels are well-defined across all phases. |
| 4 | RACI & EXECUTION READINESS | RACI missing or incomplete. FTE commitments not defined. | RACI included but lacks detail, and FTE estimates are not at an executable level. | Fully developed RACI, with clear ownership and detailed FTE commitments by phase. |
| 5 | TEAM CONTINUITY / KEY-PERSON COMMITMENT | No assurance the named team will deliver. | Named team shown but no commitment to continuity (bait-and-switch risk). | Explicit commitment that the proposed A-team will deliver, with continuity safeguards. |

### 6. BRANDING (6 criteria, weight 0.05)

**What this category covers:**

The proposal maintains high professional and brand standards, ensuring:

- Proper use of Bristlecone (or client-required) templates and layouts, including accurate logos.
- Clean, visually appealing design aligned with Bristlecone’s branding guidelines.
- Effective use of slide layouts to enhance storytelling rather than just presenting information.
- High-quality, impactful visuals that support and strengthen the message.
- Storytelling reinforces Bristlecone’s position as an SCM and AI expert, with clear differentiation.
- External validation included, such as analyst reports, awards, recognitions, published thought leadership, and earned media, to reinforce Bristlecone’s credibility.
- Spelling and grammar have been reviewed, ensuring a polished and professional final product (Hint: Use ChatGPT for proofreading).

Material should also be optimized for the intended format:

- Written Submission → More detailed, text-heavy content.
- Oral Presentation → More visuals, lighter on text, engaging delivery.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | TEMPLATE & BRAND COMPLIANCE | Template is misaligned with Bristlecone or client standards. | Template is used correctly, but branding inconsistencies exist. | Fully compliant with Bristlecone standards, visually polished, and professional. |
| 2 | STORYTELLING & POSITIONING | Bristlecone’s expertise is unclear or weakly presented. | Expertise is communicated but relies on ‘telling’ rather than strong proof points. | Clear, compelling storytelling that reinforces Bristlecone’s AI-first, consulting-led SCM leadership. |
| 3 | USE OF VISUALS | Poor-quality or irrelevant images that do not enhance storytelling. | Some visuals included but not fully optimized for impact. | Powerful, high-quality images and visuals that enhance and support messaging. |
| 4 | EXTERNAL VALIDATION & PROOF | No citations, analyst reports, awards, or third-party credibility sources. | Some external content included, but lacks strong validation. | Strong supporting materials (analyst reports, awards, recognitions, thought leadership) reinforcing credibility. |
| 5 | SPELLING, GRAMMAR & READABILITY | Multiple errors in spelling, grammar, or formatting. | Mostly clean but may have minor errors or inconsistencies. | Fully proofed, polished, and easy to read with no distracting errors. |
| 6 | FORMAT OPTIMIZATION | Not adjusted for intended delivery format (text-heavy presentation or too visual for a written submission). | Some consideration given, but not fully optimized for audience engagement. | Well-structured for intended use – concise and visual for presentations, detailed for written submission. |

**Assessing this category.** Criteria 1, 3, 5 and 6 are visual judgements. Extracted slide text carries no fonts, layout, logos or colour, so rate these from the `[Slide Images]` vision descriptions the extractor attaches to each slide. If a deck has no `[Slide Images]` text at all, return `confidence: "low"` and say in the comment that no visual signal was available — do not infer a rating from prose alone.

### 7. EXECUTIVE COMMITMENT (5 criteria, weight 0.05)

**What this category covers:**

The proposal effectively conveys Bristlecone’s commitment to the success of the program by:

- Clearly articulating executive sponsorship and involvement from leadership.
- Defining resource commitments in quantifiable terms (e.g., dedicated FTEs, SMEs, workshops, governance structure).
- Ensuring pricing strategy discussions have factored in executive participation, SME availability, and overall engagement level.
- Highlighting any outcome-based opportunities, including estimated impact and shared success models.
- Scaling commitment appropriately based on the deal size and strategic importance to both Bristlecone and the client.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | CLARITY OF EXECUTIVE INVOLVEMENT | Executive commitment is missing or vaguely referenced. | Commitment is mentioned but lacks depth or quantification. | Clearly defined executive participation, including named sponsors and their roles with specific and measurable commitment. |
| 2 | RESOURCE & SME COMMITMENT | No mention of resource commitments. | Some resource commitment is included, but not quantified. | Quantifiable commitment (e.g., specific FTEs, SMEs, workshops) with clear alignment to program success. |
| 3 | PRICING & STRATEGIC ALIGNMENT | No consideration of pricing strategy in relation to commitment. | Some consideration given to pricing strategy in relation to commitment. | Executive and resource commitments are fully factored into pricing strategy. |
| 4 | OUTCOME-BASED OPPORTUNITIES | No mention of outcome-based models or value estimation. | Some outcome-based opportunities included, but not clearly defined. | Estimated outcomes and shared success models are presented with supporting rationale. |
| 5 | STRATEGIC DEAL ALIGNMENT | Commitment feels generic and not scaled to the deal’s importance. | Some alignment, but lacks depth in demonstrating mutual value. | Commitment is scaled appropriately, ensuring a win-win for both Bristlecone and the client. |

### 8. REFERENCES & TESTIMONIALS (4 criteria, weight 0.05) — category may be N/A

**What this category covers:**

The proposal includes high-quality, relevant references that go beyond a basic logo slide, ensuring:

- Deeply relevant case studies showcasing similar or applicable work.
- Executive testimonials or quotes that reinforce key differentiators and value generated (not just generic praise like "worked hard" or "delivered on time and on budget").
- Clear business value articulation, with quantified benefits demonstrating impact.
- Alignment with partnerships, where applicable, to showcase successful collaboration.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | CASE STUDY DEPTH & RELEVANCE | Only includes a generic industry/technology logo slide. | Case studies included but lack strong alignment or customization to the deal. | Case studies are highly relevant, directly aligned with the deal scope. |
| 2 | REFERENCE VALUE PROOF | No quantified outcome in any cited reference or case study. | References cite business impact but without specific metrics or client attribution. | Each cited reference includes a quantified, client-attributed business outcome (e.g., '22% reduction in inventory' at [Client]). |
| 3 | TESTIMONIALS & EXECUTIVE QUOTES | No quotes or only generic statements that don’t reinforce differentiators. | Quotes included but not directly tied to key differentiators. | Strong, strategic quotes that reinforce Bristlecone’s unique value in this engagement. |
| 4 | PARTNERSHIP VALIDATION (IF APPLICABLE) | No mention of joint success with a relevant partner. | Some references to partnerships, but lacks clear proof of success. | Demonstrates strong collaboration and partnership success where applicable. |

### 9. SOLUTION (4 criteria, weight 0.12)

**What this category covers:**

The proposed solution is comprehensive, tailored to the client, and integrates People, Process, and Technology holistically. It clearly defines scope: geographies, organizations, functional modules, customizations, number of users, and deployment approach.

People: Organizational Change Management (OCM), governance framework, training for program team and end users.

Technology: Functional and technical scope — custom developments, integrations, testing, cutover, data migrations, and deployment strategy (Waterfall, Agile, or Global Template).

Process: Process assessments, KPIs, benchmarking, documentation, re-engineering, and operating model recommendations.

AI Differentatiors, Assets, Agents to enhance delivery, automation, insights, and decision-making

Business Value: A clear connection between how the solution is executed and the measurable outcomes it delivers.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | SOLUTION COMPLETENESS | Major components (People, Process, or Technology) are missing or weakly defined. | Covers all three areas but with minimal tailoring; could be reused for any client. | Fully developed across People, Process, and Technology with clear AI integration and client-specific tailoring. |
| 2 | CRITICAL SUCCESS FACTORS | No mention of key success drivers for implementation. | Some general success factors included, but not linked to the client’s situation. | Success factors are well-defined and contextualized to the client’s specific transformation journey. |
| 3 | BUSINESS VALUE DEFINITION | No clear connection between solution and expected outcomes. | Some business value mentioned, but weak connection between "how" and "why." | Business value is well-defined and directly linked to each component of the solution. |
| 4 | METHODOLOGY DEFINITION | Methodology is missing or vaguely described. | Methodology is included but lacks differentiation or rationale. | Clear, well-defined methodology with a Bristlecone PoV on why it’s the best fit. |

### 10. METHODOLOGY, TOOLS & ACCELERATORS (4 criteria, weight 0.08)

**What this category covers:**

The proposal clearly defines Bristlecone's recommended methodology along with relevant tools and accelerators, ensuring a comprehensive approach across People, Process, and Technology at every phase of the program.

Methodology: The recommended approach is well-documented and, where applicable, serves as a differentiator (e.g., Global Template vs. Localized Deployments). A Bristlecone Point of View (PoV) is included to justify the methodology choice.

Tools & Accelerators: The proposal identifies all relevant tools across the entire program lifecycle. These may be partner-provided, but the proposal should clarify Bristlecone’s experience with these tools and any enhancements or customizations we have developed.

Proof & Value Quantification: Specific Bristlecone accelerators should be backed by quantified benefits, such as time savings, KPI improvements, or efficiency gains. For high-impact accelerators, case studies or example clients should be referenced to demonstrate real-world value.

Comprehensive Coverage: The proposal ensures that no major methodology, tool, or accelerator is missing from the defined scope. If partner-specific tools are critical to program success, they should be explicitly included.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | METHODOLOGY DEFINITION | Methodology is missing or vaguely described. | Methodology is included but lacks differentiation or rationale. | Clear, well-defined methodology with a Bristlecone PoV on why it’s the best fit. |
| 2 | TOOLS & ACCELERATORS COVERAGE | Tools and accelerators are missing or incomplete. | Tools and accelerators are listed but lack examples or differentiation. | Comprehensive list covering all phases of the program, including relevant partner tools. |
| 3 | BRISTLECONE DEFINITION | No mention of Bristlecone’s unique enhancements or experience. | Some differentiation included but lacks clarity on investments or enhancements. | Strong positioning of Bristlecone’s differentiators, including proprietary enhancements. |
| 4 | PROOF & QUANTIFICATION | No supporting evidence or quantified benefits. | Some case studies or proof points, but lacks quantified value. | Case studies, sample outputs, or KPI impact estimates demonstrating measurable value. |

**Note on criterion 1.** `METHODOLOGY DEFINITION` is byte-identical to criterion 4 of category 9 (SOLUTION). Score `9.4` first and carry that verdict here unless the deck gives you a specific reason to diverge. The two must never contradict each other.

### 11. AI & INNOVATION (4 criteria, weight 0.07)

**What this category covers:**

AI positioned as a core, embedded differentiator — not a buzzword. Articulates an AI strategy mapped to client outcomes, names specific AI accelerators/tools (and Bristlecone enhancements), quantifies AI-driven value, and offers a forward innovation roadmap reflecting Bristlecone's AI-first, consulting-led SCM positioning.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | AI STRATEGY & EMBEDDING | AI absent or superficial. | AI referenced but not integrated into the solution. | AI seamlessly embedded across the solution, mapped to specific client outcomes. |
| 2 | AI ACCELERATORS & TOOLS | No AI tools named. | Tools named but no Bristlecone differentiation. | Specific AI accelerators/tools detailed, including Bristlecone's proprietary enhancements. |
| 3 | AI PROOF & OUTCOMES | No evidence of AI value. | Claims made without quantification. | Quantified AI outcomes (efficiency, accuracy, speed) backed by examples/case studies. |
| 4 | INNOVATION & FUTURE ROADMAP | No forward view. | Generic innovation statements. | Clear innovation roadmap showing evolving value beyond the initial scope. |

### 12. COMMERCIALS, PRICING & INVESTMENT (4 criteria, weight 0.08)

**What this category covers:**

Presents a transparent, well-structured commercial model: clear pricing and cost breakdown, assumptions/exclusions, payment milestones, pricing options where relevant (T&M / fixed / outcome-based), connected to the value delivered.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | PRICING TRANSPARENCY | Pricing missing or opaque (lump sum, no breakdown). | Price given but limited breakdown or unclear basis of estimate. | Transparent cost breakdown by phase/workstream/role with clear basis of estimate. |
| 2 | COMMERCIAL MODEL & OPTIONS | Single rigid price, no options. | One model; limited flexibility. | Clear model with options/tiers (T&M, fixed, outcome-based) suited to client risk appetite. |
| 3 | ASSUMPTIONS & EXCLUSIONS | None stated; scope-for-price unclear. | Some assumptions noted but incomplete. | Assumptions, exclusions, and change mechanism clearly defined to protect both parties. |
| 4 | VALUE-FOR-MONEY / ROI LINKAGE | Price not linked to value. | Loose linkage between cost and benefit. | Investment clearly justified against quantified business value / ROI. |

### 13. COMPETITIVE DIFFERENTIATION / WIN THEMES (3 criteria, weight 0.07)

**What this category covers:**

Establishes clear win themes and differentiators, positioning Bristlecone advantageously against likely alternatives and reinforcing why Bristlecone is uniquely the right partner for this specific client and deal.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | WIN THEMES | No discernible win themes. | Themes implied but not explicit or consistent. | 3–4 explicit, client-resonant win themes threaded through the proposal. |
| 2 | COMPETITIVE POSITIONING | No differentiation vs. alternatives. | Generic strengths, not comparative. | Clear, evidence-backed differentiation against likely competitors/alternatives. |
| 3 | WHY BRISTLECONE | Generic or missing. | Stated but not compelling or specific. | Compelling, client-specific case for Bristlecone tied to proven outcomes. |

### 14. RISKS, ASSUMPTIONS & MITIGATION (3 criteria, weight 0.03)

**What this category covers:**

Proactively surfaces key delivery, technical, organizational and commercial risks, with mitigation strategies, dependencies and assumptions — demonstrating delivery maturity and credibility.

| # | Criterion | Does Not Meet | Minimally Meets | Best Practice |
|---|---|---|---|---|
| 1 | RISK IDENTIFICATION | No risks identified, or only a generic risk-management process with no named risks. | Some risks named, but incomplete, unprioritised, or not specific to this engagement. | Key delivery, technical, organizational and commercial risks are named, prioritised, and specific to this client and scope. |
| 2 | MITIGATION PLANNING | No mitigations stated. | Mitigations mentioned generically, or not tied to the risks that were named. | Each material risk has a specific, credible mitigation with a named owner and a trigger. |
| 3 | DEPENDENCIES & ASSUMPTIONS | No dependencies or assumptions stated; delivery preconditions are unclear. | Some assumptions or dependencies listed, but incomplete or not tied to scope and schedule. | Dependencies and assumptions documented per workstream, including client obligations and the consequence if they are not met. |

**Scoring legend, as printed in the source workbook:** Scoring: ✅ Best Practice = 2  |  ⚠️ Minimally Meets = 1  |  ❌ Does Not Meet = 0  |  Not Applicable = excluded. Set the rating on each criterion in the 'Pitch Scorecard' tab; scores roll up here automatically. Green rows = newly recommended categories.


---

## 3. Response contract

Return one JSON object per category request, no prose around it:

```json
{"criteria": [
  {"id": "12.1",
   "rating": "Does Not Meet",
   "evidence_status": "unassessable",
   "slides": [{"doc": "Semtech_Proposal.pptx", "slide": 87}],
   "quote": "Cost: xxx",
   "comment": "Every cost line in the Commercials table (slide 87) is a literal 'xxx' placeholder, so there is no breakdown or basis of estimate to assess.",
   "to_improve": "Populate slide 87 with cost by phase and workstream, and state the basis of estimate.",
   "confidence": "high"}
]}
```

Field rules:
- `id` — exactly as declared in section 2. One entry per criterion in the requested
  category, no extras, no omissions.
- `rating` — one of `Best Practice`, `Minimally Meets`, `Does Not Meet`,
  `Not Applicable`. Nothing else.
- `evidence_status` — `found`, `absent` or `unassessable` (Rule 2).
- `slides` — the slides the rating rests on, each `{"doc": ..., "slide": N}`. Empty
  array when `evidence_status` is `absent`.
- `quote` — a short verbatim fragment from the deck supporting the rating, or `""`.
  Never invent or paraphrase a quote.
- `comment` — the reviewer-facing finding. Specific, cites its slides, names what is
  wrong rather than restating the anchor.
- `to_improve` — the concrete fix (Rule 7). `""` only when the rating is `Best Practice`.
- `confidence` — `high`, `medium` or `low`. Use `low` when the evidence is thin or the
  judgement is visual and no `[Slide Images]` text was available.

---

## 4. Changelog and data fixes

### DATA FIX — category 14 anchors (2026-09-22)
In `Pitch Checklist_v1.3.xlsx` **and** `Scorecard sample.xlsx`, rows 58-60, all three
`RISKS, ASSUMPTIONS & MITIGATION` criteria carry the anchor text of row 54
(`VALUE-FOR-MONEY / ROI LINKAGE`): *"Price not linked to value."* / *"Loose linkage
between cost and benefit."* / *"Investment clearly justified against quantified business
value / ROI."*

That is a copy-paste defect in the source template, not a deliberate rubric choice — the
anchors describe pricing while the criteria are risk identification, mitigation planning
and dependencies. Corrected anchors were authored for this file.

**Action required:** the replacements need sign-off from the rubric owner (Sales
Excellence Team), and v1.4 of the workbook should be fixed at source so the spreadsheet
and this file do not drift.

### KNOWN REDUNDANCY — METHODOLOGY DEFINITION is scored twice
Criterion `9.4` (SOLUTION) and criterion `10.1` (METHODOLOGY, TOOLS & ACCELERATORS) share
a name and byte-identical anchors. Ids are category-scoped so they never collide, but it
is one judgement counted twice at a combined weight of `0.12/4 + 0.08/4 = 0.05`. Score
`9.4` first and carry the verdict into `10.1`. Worth raising with the rubric owner.
