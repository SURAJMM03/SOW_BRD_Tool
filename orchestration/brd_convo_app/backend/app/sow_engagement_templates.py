"""
sow_engagement_templates.py — Engagement-type-specific SOW templates (US-01)

Two standardized, version-controlled SOW templates: one for Time & Materials
engagements and one for Fixed Cost engagements. A SOW's commercial and
acceptance language has to match how the work is actually billed — a T&M SOW
that promises milestone-triggered payments, or a Fixed Cost SOW that bills
against approved timesheets, is wrong in a way a reviewer has to catch by
hand. Choosing the engagement type at the start of drafting makes the right
language the default instead of a correction.

WHAT "VERSION-CONTROLLED" MEANS HERE
────────────────────────────────────
The templates are code, so git is the version history — every change to a
section list or a piece of drafting guidance is a reviewable diff. On top of
that each template carries an explicit `version` string, which is:
  * returned by GET /api/sow-templates/engagement-types,
  * stamped onto the project when the template is applied
    (`engagement_template_version` in the project's sections store), and
  * therefore visible for any draft, so you can tell which revision of the
    template produced it.
Bump `version` (and add a CHANGELOG line) whenever sections or guidance
change in a way that would alter an already-generated SOW.

CHANGELOG
─────────
1.0.0  Initial T&M and Fixed Cost templates.
1.1.0  US-04: added 4.7 Exclusions and 4.8 Dependencies to both templates.
1.2.0  US-05/US-06: measurable acceptance criteria, explicit review windows and
       automatic deemed acceptance; full CR workflow, approvers, turnaround,
       mandatory impact assessment and no-work-before-approval.
1.3.0  US-07: credit period in days, delayed-payment consequence, currency, tax
       and expense treatment mandatory in both templates.
       US-08: added 7.1-7.5 (governance forums, roles, work schedule, escalation
       matrix, key contacts).
       US-10: added 14.1 SLAs, Targets & Breach Consequences.
1.4.0  US-11/US-12: added 8.1 Onboarding & Access Provisioning and 8.2 Asset
       Management & Return.
       US-13: section 19 becomes Term & Termination with 19.1-19.4.
       US-14/US-15: added 12.3 Subcontractor & Vendor Alignment and 12.4
       Contractor & Vendor Compliance.

HOW THIS PLUGS INTO GENERATION
──────────────────────────────
`sections`   seeds the project's section tree (same shape as
             sow_template_routes.FALLBACK_SECTIONS).
`guidance`   overrides/extends sow_section_routes.SECTION_GUIDANCE per
             section id — this is what actually changes the drafted prose.
`table_ids`  extends sow_template_routes.TABLE_SECTION_IDS, for the
             engagement-specific sections that must render as tables.
`premise`    one line injected into every section's prompt, so even sections
             with no engagement-specific guidance of their own (Executive
             Summary, Assumptions, Change Management …) describe the right
             billing model when they touch on it.

A client's own uploaded .docx template can also be applied with an
engagement type: the section ids won't match `guidance` (it numbers things
its own way), but `premise` still applies to every section, so the billing
model stays consistent.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

TIME_AND_MATERIALS = "tm"
FIXED_COST = "fixed_cost"

# Sections shared by both engagement types. The two templates differ only
# where the engagement model genuinely changes the document — Key
# Deliverables, Acceptance Criteria and Commercials — so this list stays the
# single place a structural change to the rest of the SOW has to be made.
_BASE_SECTIONS: List[Dict] = [
    {"id": "cover", "title": "Cover Page",                                      "level": 1},
    {"id": "1",     "title": "Agreement Details",                               "level": 1},
    {"id": "2",     "title": "Executive Summary",                               "level": 1},
    {"id": "3",     "title": "Project Overview",                                "level": 1},
    {"id": "4",     "title": "Scope of Work",                                   "level": 1},
    {"id": "5",     "title": "Proposed Architecture",                           "level": 1},
    {"id": "6",     "title": "Solution Implementation Approach & Project Plan", "level": 1},
    {"id": "7",     "title": "Project Management",                              "level": 1},
    {"id": "8",     "title": "Personnel Requirements",                          "level": 1},
    {"id": "9",     "title": "Key Deliverables",                                "level": 1},
    {"id": "10",    "title": "Key Assumptions",                                 "level": 1},
    {"id": "11",    "title": "Acceptance Criteria",                             "level": 1},
    {"id": "12",    "title": "Obligations",                                     "level": 1},
    {"id": "13",    "title": "Commercials",                                     "level": 1},
    {"id": "14",    "title": "Performance Reporting & KPIs",                    "level": 1},
    {"id": "15",    "title": "Risks",                                           "level": 1},
    {"id": "16",    "title": "Governance & Risk Mitigation",                    "level": 1},
    {"id": "17",    "title": "Change Management Process",                       "level": 1},
    {"id": "18",    "title": "Security & Data Protection",                      "level": 1},
    {"id": "19",    "title": "Term & Termination",                                     "level": 1},
    {"id": "20",    "title": "Acceptance and Sign-Off",                         "level": 1},
    {"id": "21",    "title": "Key Observations",                                "level": 1},
    {"id": "22",    "title": "Reviewer Recommendations",                        "level": 1},
    {"id": "appA",  "title": "Appendix A — Technical Prerequisites",            "level": 1},
    {"id": "appB",  "title": "Appendix B — Resource Profiles & Certifications", "level": 1},
]

# ── Time & Materials ────────────────────────────────────────────────────────
# Billing follows effort actually expended at agreed rates, so the commercial
# spine is the rate card, the effort estimate, the not-to-exceed ceiling and
# timesheet approval. Acceptance governs deliverable quality but does NOT
# gate payment — approved timesheets do.
_TM_SUBSECTIONS: Dict[str, List[tuple]] = {
    "9": [("9.1", "Deliverables Register"),
          ("9.2", "Estimated Effort by Deliverable")],
    "11": [("11.1", "Deliverable Acceptance"),
           ("11.2", "Timesheet Review & Approval")],
    "13": [("13.1", "Engagement Model — Time & Materials"),
           ("13.2", "Rate Card & Pyramid Structure"),
           ("13.3", "Estimated Effort & Not-to-Exceed Ceiling"),
           ("13.4", "Timesheet Submission & Approval"),
           ("13.5", "Invoicing & Payment Terms"),
           ("13.6", "Travel & Expenses"),
           ("13.7", "Rate Revision")],
}

_TM_GUIDANCE: Dict[str, str] = {
    "9": "Key Deliverables — this is a Time & Materials engagement: deliverables define the "
         "work product and its quality bar, they do NOT define payment. Introduce the register "
         "in one sentence and say plainly that fees are based on effort expended at the agreed "
         "rates, not on deliverable completion.",
    "9.1": "Deliverables Register — table (# | Deliverable | Description | Responsible Party | "
           "Target Date). Target dates are planning dates, not payment triggers; say so in one "
           "line under the table.",
    "9.2": "Estimated Effort by Deliverable — table (Deliverable | Role/Band | Estimated "
           "Person-Days | Assumptions). State that estimates are for planning and budget "
           "tracking; actual billable effort is what is recorded and approved on timesheets.",
    "11": "Acceptance Criteria — this is a Time & Materials engagement. Acceptance governs "
          "whether a deliverable meets its quality bar and whether rework is needed; it does "
          "NOT gate invoicing. Say explicitly that invoices are raised against approved "
          "timesheets regardless of deliverable acceptance status, and that defects are "
          "remediated as additional billable effort unless caused by Bristlecone's failure to "
          "meet an agreed standard.",
    "11.1": "Deliverable Acceptance — table (Deliverable | Measurable Acceptance Criteria | "
            "Reviewer | Review Window). Criteria must be objectively checkable per "
            "deliverable — a named test, threshold or document state, never 'client "
            "satisfaction'. The Review Window must be an explicit number of days stated as "
            "BUSINESS or CALENDAR days. Immediately after the table state the "
            "deemed-acceptance rule as its own sentence: absent a written rejection naming "
            "the failed criterion within the review window, the deliverable is deemed "
            "accepted on expiry of that window. Note that on this T&M engagement deemed "
            "acceptance closes the quality question only — it does not gate invoicing.",
    "11.2": "Timesheet Review & Approval — the payment-critical control on a T&M engagement. "
            "Bullets covering: submission frequency (weekly), the named client approver, the "
            "approval window, what happens on non-response (deemed approved), and the dispute "
            "route for contested entries. Use a <CONFIRM: …> placeholder for the approver name "
            "and the window if the source material does not state them.",
    "13": "Commercials — Time & Materials. Open with one sentence: fees are calculated as "
          "actual effort expended multiplied by the agreed role rates, invoiced monthly in "
          "arrears against client-approved timesheets, subject to the not-to-exceed ceiling. "
          "Never describe milestone-based or deliverable-based payment in this section.",
    "13.1": "Engagement Model — Time & Materials. State the model in one line and name what is "
            "billable (effort at agreed rates) and what is not (unapproved effort, rework "
            "arising from Bristlecone's own defects). Do not offer a fixed price.",
    "13.2": "Rate Card & Pyramid Structure — table (Role/Band | Location | Rate USD/day | Rate "
            "USD/hour | Blended %). This table is the commercial core of a T&M SOW; if the "
            "source material gives no rates, emit a <CONFIRM: …> placeholder row rather than "
            "inventing numbers.",
    "13.3": "Estimated Effort & Not-to-Exceed Ceiling — table (Phase/Workstream | Role | "
            "Estimated Person-Days | Extended Value). Follow it with the ceiling: the total "
            "value that may not be exceeded without a signed change request, and the "
            "notification threshold (e.g. Bristlecone notifies at 80% consumption).",
    "13.4": "Timesheet Submission & Approval — how effort becomes billable: submission cadence, "
            "approver, approval window, deemed-approval rule, and the evidence attached to each "
            "invoice.",
    "13.5": "Invoicing & Payment Terms — monthly in arrears for effort approved in that "
            "period, with the approved timesheet summary as invoice backup. All five of these "
            "are mandatory: (1) the billing model restated in one clause — Time & Materials; "
            "(2) the credit period as an explicit number of days from invoice date "
            "(e.g. \"net thirty (30) days\"); (3) a delayed-payment clause naming the "
            "consequence — an interest rate per annum or per month on overdue amounts, and/or "
            "the right to suspend services after a stated notice period; (4) the invoicing "
            "currency; (5) the tax treatment (whether amounts are exclusive of VAT/GST/"
            "withholding and who bears them). Use <CONFIRM: …> for any figure the source "
            "material does not give — never invent a rate or a payment term. Do not reference "
            "milestones.",
    "13.6": "Travel & Expenses — state the expense treatment explicitly: whether travel and "
            "out-of-pocket expenses are billed in addition to fees or included in the rates, "
            "reimbursement at actuals against receipts, the pre-approval threshold, and any "
            "per-diem or travel-policy cap.",
    "13.7": "Rate Revision — when rates may be revised (typically annually), the notice period, "
            "and that revisions apply only to effort after the effective date.",
    "17": "Change Management Process — on a T&M engagement a change request is needed to raise "
          "the not-to-exceed ceiling, to add roles not on the rate card, or to change agreed "
          "rates. Scope changes within the ceiling are handled by re-prioritisation and "
          "recorded, not repriced. Give the workflow as a table (Step | Activity | Owner | "
          "Turnaround) over the six-step flow (raised → logged → impact assessment → "
          "reviewed/approved → SOW amendment → closure), with a turnaround in business days "
          "on every step; name the approver role on both sides and who holds final authority; "
          "state that a commercial and timeline impact assessment is mandatory for every CR "
          "(quantifying additional effort in person-days and its ceiling impact, even when "
          "nil); and state that no work on a change starts before the CR is approved in "
          "writing, with unapproved effort not chargeable.",
    "20": "Acceptance and Sign-Off — signature block for the SOW itself. Note that SOW signature "
          "authorises effort against the ceiling; it is not acceptance of any deliverable.",
}

_TM_TABLE_IDS: Set[str] = {"9.1", "9.2", "11.1", "13.2", "13.3"}

# ── Fixed Cost ──────────────────────────────────────────────────────────────
# A single agreed price buys an agreed scope. Payment is earned by completing
# milestones, and milestone completion is proven by deliverable acceptance —
# so acceptance IS the payment trigger, and scope control is what protects
# the price.
_FC_SUBSECTIONS: Dict[str, List[tuple]] = {
    "9": [("9.1", "Deliverables Register"),
          ("9.2", "Deliverable-to-Milestone Mapping")],
    "11": [("11.1", "Deliverable Acceptance Criteria"),
           ("11.2", "Milestone Sign-off & Payment Trigger")],
    "13": [("13.1", "Engagement Model — Fixed Cost"),
           ("13.2", "Total Contract Value"),
           ("13.3", "Milestone Payment Schedule"),
           ("13.4", "Invoicing & Payment Terms"),
           ("13.5", "Travel & Expenses"),
           ("13.6", "Change Requests & Cost Impact")],
}

_FC_GUIDANCE: Dict[str, str] = {
    "9": "Key Deliverables — this is a Fixed Cost engagement: every deliverable must be "
         "attributable to a milestone, because milestones are what release payment. Introduce "
         "the register in one sentence and state that the fee is fixed for exactly this set of "
         "deliverables.",
    "9.1": "Deliverables Register — table (# | Deliverable | Description | Milestone | "
           "Responsible Party | Due Date). Every row must name the milestone it belongs to; a "
           "deliverable with no milestone is a scope leak.",
    "9.2": "Deliverable-to-Milestone Mapping — table (Milestone | Deliverables Included | "
           "Completion Definition | % of Total Contract Value). The percentages must total "
           "100%; if the source material does not support a split, emit a <CONFIRM: …> "
           "placeholder rather than inventing one.",
    "11": "Acceptance Criteria — this is a Fixed Cost engagement, so acceptance is the payment "
          "trigger: a milestone is invoiceable only once its deliverables are accepted. Say "
          "that explicitly, and state that remediation of defects against the agreed criteria "
          "is included in the fixed fee (no additional charge).",
    "11.1": "Deliverable Acceptance Criteria — table (Deliverable | Milestone | Acceptance "
            "Criteria | Reviewer | Review Window). Criteria must be objective and testable — "
            "on a fixed-price engagement a vague criterion is an unbounded obligation.",
    "11.2": "Milestone Sign-off & Payment Trigger — the commercial hinge of a Fixed Cost SOW. "
            "Bullets covering: who signs off a milestone; the review window as an explicit "
            "number of BUSINESS or CALENDAR days; the deemed-acceptance rule — if no written "
            "rejection naming the failed criterion is issued within that window the milestone "
            "is deemed accepted automatically on expiry and becomes invoiceable; what a "
            "rejection must specify in writing; the re-submission cycle and its own review "
            "window; and the statement that a signed milestone certificate authorises the "
            "corresponding invoice. Deemed acceptance must be stated as automatic — a "
            "milestone cannot sit unapproved indefinitely and block payment.",
    "13": "Commercials — Fixed Cost. Open with one sentence: the engagement is delivered for a "
          "fixed total price covering the scope in Section 4, invoiced against completed and "
          "accepted milestones. Never describe rate-based or effort-based billing in this "
          "section — rates, if shown at all, appear only as change-request pricing.",
    "13.1": "Engagement Model — Fixed Cost. State that the fee is fixed for the defined scope "
            "and that Bristlecone carries the effort risk within that scope. Name what would "
            "change the price (a signed change request) and what would not (internal effort "
            "variance).",
    "13.2": "Total Contract Value — the single fixed price, currency, and precisely what it "
            "includes and excludes. If the source material gives no figure, emit a "
            "<CONFIRM: …> placeholder — never estimate a contract value.",
    "13.3": "Milestone Payment Schedule — table (Milestone | Completion Criteria | Target Date "
            "| % of Total | Amount). Percentages must total 100% and amounts must reconcile to "
            "the Total Contract Value. This table is the commercial core of a Fixed Cost SOW.",
    "13.4": "Invoicing & Payment Terms — invoices are raised on milestone acceptance, "
            "supported by the signed milestone certificate. All five of these are mandatory: "
            "(1) the billing model restated in one clause — Fixed Cost, invoiced against "
            "accepted milestones; (2) the credit period as an explicit number of days from "
            "invoice date (e.g. \"net thirty (30) days\"); (3) a delayed-payment clause "
            "naming the consequence — an interest rate per annum or per month on overdue "
            "amounts, and/or the right to suspend services after a stated notice period; "
            "(4) the invoicing currency; (5) the tax treatment (whether amounts are exclusive "
            "of VAT/GST/withholding and who bears them). Use <CONFIRM: …> for any figure the "
            "source material does not give. Do not reference timesheets or hourly rates.",
    "13.5": "Travel & Expenses — state the expense treatment explicitly: whether travel and "
            "out-of-pocket expenses are included in the fixed price or reimbursed separately "
            "at actuals against receipts; if separate, give the pre-approval threshold and any "
            "cap. Silence here is a collections dispute waiting to happen.",
    "13.6": "Change Requests & Cost Impact — the mechanism that protects the fixed price. "
            "Table (Step | Activity | Owner | Turnaround) for the six-step CR workflow "
            "(raised → logged → impact assessment → reviewed/approved → SOW amendment → "
            "closure), with an explicit turnaround in business days on every step. Name the "
            "approver role on both sides and who holds final authority. State that a "
            "commercial AND timeline impact assessment is mandatory for every CR — quantified "
            "against the Total Contract Value and the milestone schedule, even when the impact "
            "is nil — and that no work outside the agreed scope begins before a CR is signed "
            "by both parties, with unapproved work neither chargeable nor a basis for moving "
            "a milestone date. Day rates appear here only as the valuation basis for changes.",
    "10": "Key Assumptions — on a Fixed Cost engagement, assumptions are price-protective: each "
          "one should make clear what Bristlecone has priced for. Bullets, and for any "
          "assumption whose failure would change the price, say so explicitly (\"if X is not "
          "available by <date>, a change request applies\").",
    "17": "Change Management Process — on a Fixed Cost engagement every scope change is priced "
          "and requires a signed change request before work starts. Describe the workflow, the "
          "approval authority on both sides, and the effect on the milestone schedule and "
          "Total Contract Value.",
    "20": "Acceptance and Sign-Off — signature block for the SOW itself, plus a pointer to the "
          "milestone certificate as the instrument that evidences delivery acceptance.",
}

_FC_TABLE_IDS: Set[str] = {"9.1", "9.2", "11.1", "13.3", "13.6"}


# US-04 — scope boundaries. Both engagement types need the same five areas
# defined: Scope ("4"), Deliverables ("9"), Assumptions ("10"), and these two,
# which had no section of their own before. The ids continue the branded
# template's own 4.x numbering (4.1 Geographical … 4.7 Exclusions) rather than
# inventing a parallel scheme, so "4.7" means Exclusions in every template and
# SECTION_GUIDANCE stays keyed consistently.
_SCOPE_SUBSECTIONS: Dict[str, List[tuple]] = {
    "4": [("4.7", "Exclusions (Out of Scope)"),
          ("4.8", "Dependencies")],
    # US-08 — governance is only real with forums, an escalation ladder and
    # contactable people. 7.1-7.3 keep the branded template's meanings;
    # 7.4/7.5 extend past where that template stops.
    "7": [("7.1", "Governance Model & Forums"),
          ("7.2", "Roles & Responsibilities"),
          ("7.3", "Work Schedule"),
          ("7.4", "Escalation Matrix"),
          ("7.5", "Key Contacts")],
    # US-10 — the branded template already numbers SLAs & Targets as 14.1.
    "14": [("14.1", "SLAs, Targets & Breach Consequences")],
    # US-11 / US-12 — people cannot bill before they can log in, and issued
    # kit has to come back. Both hang off Personnel Requirements.
    "8": [("8.1", "Onboarding & Access Provisioning"),
          ("8.2", "Asset Management & Return")],
    # US-14 / US-15 — vendor topics sit with the other party obligations.
    "12": [("12.1", "Bristlecone Obligations"),
           ("12.2", "Client Obligations"),
           ("12.3", "Subcontractor & Vendor Alignment"),
           ("12.4", "Contractor & Vendor Compliance")],
    # US-13 — Term belongs in front of Termination, so these ids carry the
    # engagement templates' own meanings. The branded template numbers 19.1-19.3
    # differently; _COMMON_GUIDANCE below overrides them for engagement projects
    # only, leaving branded projects on SECTION_GUIDANCE as before.
    "19": [("19.1", "Term, Duration & Renewal"),
           ("19.2", "Termination for Convenience"),
           ("19.3", "Termination for Cause"),
           ("19.4", "Exit Obligations")],
}
# The dependencies table carries an owning-party column, so it must render as
# a table on both templates.
_SCOPE_TABLE_IDS: Set[str] = {"4.8", "7.1", "7.2", "7.4", "7.5", "14.1",
                              "8.1", "8.2", "12.3"}


# Guidance that is identical for both engagement types but has to live here
# rather than in SECTION_GUIDANCE, because these ids mean something different
# in the branded template's own numbering (see the "19" note above).
_COMMON_GUIDANCE: Dict[str, str] = {
    "19": "Term & Termination — the term itself, both termination routes, and what each "
          "party owes the other on the way out. A termination clause with no exit "
          "obligations leaves knowledge, data and assets stranded.",
    "19.1": "Term, Duration & Renewal — state the start date, the end date or duration, and "
            "the renewal or extension mechanism (auto-renewal with notice, extension only by "
            "written amendment, or expiry with no renewal). Say which of those applies; do "
            "not leave renewal unaddressed. Use <CONFIRM: ...> for dates the source material "
            "does not give.",
    "19.2": "Termination for Convenience — either party may terminate without cause on an "
            "explicit written notice period, stated as a number of days. Say what is payable "
            "on such a termination (work performed and accepted to the termination date, plus "
            "any irrevocable committed costs).",
    "19.3": "Termination for Cause — the grounds (material breach, insolvency, fraud, "
            "persistent non-payment), the cure period as an explicit number of days, and the "
            "written notice required. Both parties must have the right, not only the client.",
    "19.4": "Exit Obligations — bullets covering all four, each with a timeline: "
            "(1) knowledge transfer, a documented handover and its duration; "
            "(2) data return or deletion, return of client data in an agreed format and "
            "certified deletion of remaining copies within a stated number of days; "
            "(3) asset return, all issued hardware, access cards and licences returned per "
            "the Asset Management section; (4) final settlement, the final invoice, "
            "reconciliation of prepaid or unbilled amounts, and the payment date. Omitting "
            "any one of the four is the gap this section exists to close.",
}


def _merge_subsections(*groups: Dict[str, List[tuple]]) -> Dict[str, List[tuple]]:
    """Combine sub-section groups, keeping each parent's children in the order
    the groups are given and then by id."""
    merged: Dict[str, List[tuple]] = {}
    for g in groups:
        for parent, subs in g.items():
            merged.setdefault(parent, []).extend(subs)
    for parent in merged:
        merged[parent].sort(key=lambda s: [int(p) for p in s[0].split(".")])
    return merged


def _build_sections(subsections: Dict[str, List[tuple]]) -> List[Dict]:
    """Expand _BASE_SECTIONS by inserting each parent's sub-sections directly
    after it, preserving document order. A parent that gains sub-sections
    keeps has_own_content=True: on both templates these parents carry a short
    framing sentence of their own (see the "9"/"11"/"13" guidance) before the
    first sub-section, so they are not pure containers."""
    out: List[Dict] = []
    for sec in _BASE_SECTIONS:
        out.append(dict(sec, has_own_content=True))
        for sid, title in subsections.get(sec["id"], []):
            out.append({"id": sid, "title": title, "level": 2, "has_own_content": True})
    return out


ENGAGEMENT_TEMPLATES: Dict[str, Dict] = {
    TIME_AND_MATERIALS: {
        "key": TIME_AND_MATERIALS,
        "name": "Time & Materials SOW",
        "short_label": "T&M",
        "version": "1.4.0",
        "description": "Effort billed at agreed role rates against approved timesheets, "
                       "capped by a not-to-exceed ceiling.",
        "billing_basis": "effort and rates",
        "premise": (
            "ENGAGEMENT TYPE: Time & Materials. Fees are based on actual effort expended at "
            "agreed role rates, invoiced monthly in arrears against client-approved "
            "timesheets, subject to a not-to-exceed ceiling. Deliverable acceptance governs "
            "quality and rework — it does NOT trigger payment. Wherever this section touches "
            "on commercials, billing, invoicing or acceptance, use that model and never "
            "describe milestone-based or deliverable-based payment."
        ),
        "sections": _build_sections(_merge_subsections(_SCOPE_SUBSECTIONS, _TM_SUBSECTIONS)),
        "guidance": {**_COMMON_GUIDANCE, **_TM_GUIDANCE},
        "table_ids": _TM_TABLE_IDS | _SCOPE_TABLE_IDS,
    },
    FIXED_COST: {
        "key": FIXED_COST,
        "name": "Fixed Cost SOW",
        "short_label": "Fixed Cost",
        "version": "1.4.0",
        "description": "A single fixed price for a defined scope, invoiced against completed "
                       "and accepted milestones.",
        "billing_basis": "milestones and deliverables",
        "premise": (
            "ENGAGEMENT TYPE: Fixed Cost. The engagement is delivered for a fixed total price "
            "covering the defined scope, invoiced against milestones that become payable only "
            "once their deliverables are formally accepted — acceptance IS the payment "
            "trigger. Effort variance within scope is Bristlecone's risk and never changes the "
            "price; only a signed change request does. Wherever this section touches on "
            "commercials, billing, invoicing or acceptance, use that model and never describe "
            "rate-based or timesheet-based billing."
        ),
        "sections": _build_sections(_merge_subsections(_SCOPE_SUBSECTIONS, _FC_SUBSECTIONS)),
        "guidance": {**_COMMON_GUIDANCE, **_FC_GUIDANCE},
        "table_ids": _FC_TABLE_IDS | _SCOPE_TABLE_IDS,
    },
}


def get_engagement_template(engagement_type: Optional[str]) -> Optional[Dict]:
    """The template for an engagement key, or None for an unknown/absent key.
    Callers treat None as "no engagement type chosen" and fall back to the
    engagement-neutral defaults, so an old project created before US-01 keeps
    working unchanged."""
    if not engagement_type:
        return None
    return ENGAGEMENT_TEMPLATES.get(engagement_type)


def is_valid_engagement_type(engagement_type: Optional[str]) -> bool:
    return engagement_type in ENGAGEMENT_TEMPLATES


def engagement_guidance(engagement_type: Optional[str], section_id: str) -> str:
    """Engagement-specific drafting guidance for a section id, or "" if this
    engagement type says nothing special about it. Falls back from a
    sub-section id to its parent ("13.4" -> "13") the same way
    sow_section_routes._guidance_for_section does, so a template whose
    sub-sections are not individually listed still inherits the engagement's
    framing for that area."""
    tpl = get_engagement_template(engagement_type)
    if not tpl:
        return ""
    guidance = tpl["guidance"]
    if section_id in guidance:
        return guidance[section_id]
    return guidance.get(section_id.split(".")[0], "")


def engagement_premise(engagement_type: Optional[str]) -> str:
    tpl = get_engagement_template(engagement_type)
    return tpl["premise"] if tpl else ""


def engagement_table_ids(engagement_type: Optional[str]) -> Set[str]:
    tpl = get_engagement_template(engagement_type)
    return set(tpl["table_ids"]) if tpl else set()


def list_engagement_templates() -> List[Dict]:
    """Summary of both templates for the selection UI — no section bodies."""
    return [
        {
            "key": t["key"],
            "name": t["name"],
            "short_label": t["short_label"],
            "version": t["version"],
            "description": t["description"],
            "billing_basis": t["billing_basis"],
            "section_count": len(t["sections"]),
        }
        for t in ENGAGEMENT_TEMPLATES.values()
    ]
