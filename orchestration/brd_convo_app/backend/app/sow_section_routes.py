"""
sow_section_routes.py — Section-by-section SOW generation, review, and approval.

Mirrors screen3_routes.py's generate → approve → next-section loop (the BRD
tool's proven pattern), scoped to SOW projects (workflow="sow" in
project_routes.PROJECTS) with their own per-project stores so nothing here
touches BRD's screen3_* files:

  sow_sections_{project_id}.json  — the section list (from a template or the
                                     built-in fallback), same shape as BRD's
                                     screen3_sections_*.json
  sow_approved_{project_id}.json  — approved content per section id
  sow_keyword_{project_id}.json   — per-section status + optional reviewer
                                     instructions

Endpoints:
  GET  /api/sow/sections?project_id=
  POST /api/sow/sections/{id}/generate
  POST /api/sow/sections/{id}/approve
  POST /api/sow/generation/start
  GET  /api/sow/generation/status?project_id=
  POST /api/sow/generation/stop?project_id=
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.claude_provider import completion_from_prompt
from app.image_injection import (get_relevant_images, format_images_for_context,
                                 filter_already_used,
                                 verify_image_relevance)
from app.sow_template_routes import (
    FALLBACK_SECTIONS, TABLE_SECTION_IDS, DIAGRAM_PRIORITY_SECTION_IDS,
    get_default_template,
)
from app.sow_engagement_templates import (
    engagement_guidance, engagement_premise, engagement_table_ids,
)

logger = logging.getLogger("SOWSectionRoutes")
router = APIRouter(prefix="/api/sow", tags=["sow-sections"])

_THIS_DIR = Path(__file__).parent
# Shared with image_repo_routes.py / screen3_routes.py — one physical image
# store and one BM25 index file across BRD, SOW, and the image library.
_IMAGE_CHUNKS_PATH = _THIS_DIR / "image_chunks.json"
_EXTRACTED_IMAGES_DIR = _THIS_DIR / "extracted_images"
_SOW_ATTACHMENT_UPLOAD_ROOT = _THIS_DIR / "sow_uploads"
_ATTACHMENT_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
_ATTACHMENT_DOC_EXTS = {"docx", "pdf", "pptx", "xlsx"}

# Per-section budget. A real, professionally-authored SOW section runs roughly
# 80-250 words of prose (a table section needs more room for rows) — 4000
# tokens (~3000 words) gave the model no reason NOT to pad every section into
# multiple paragraphs restating the same point, which is exactly what caused a
# 58-section draft to balloon to 150+ pages versus a real SOW's ~25-30. These
# caps are a deliberate, tight ceiling; DENSITY_RULES below does the actual
# behavioral work of keeping the model terse within that ceiling.
SOW_MAX_OUTPUT_TOKENS = 700
SOW_MAX_OUTPUT_TOKENS_TABLE = 1100
# Ceiling for a combined "family" generate call (a top-level section with
# several sub-sections drafted together in one response) — the per-block sum
# could otherwise grow unbounded for a family with many table sub-parts.
SOW_MAX_OUTPUT_TOKENS_COMBINED_CAP = 3200

# ─────────────────────────────────────────────────────────────────────────────
# SKILL FILE — same file BRD's old one-shot sow_routes.py reads, loaded once
# here too so the section-by-section engine follows the same operating
# principles, checklist knowledge, and Step 3 template structure.
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SKILL_PATH = _REPO_ROOT / "SOW_SKILL.md"
try:
    SOW_SKILL_TEXT = _SKILL_PATH.read_text(encoding="utf-8")
except Exception as exc:
    SOW_SKILL_TEXT = ""
    logger.error("Could not load SOW_SKILL.md from %s: %s", _SKILL_PATH, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Per-project store helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sections_path(pid: str) -> Path: return _THIS_DIR / f"sow_sections_{pid}.json"
def _approved_path(pid: str) -> Path: return _THIS_DIR / f"sow_approved_{pid}.json"
def _keyword_path(pid: str) -> Path:  return _THIS_DIR / f"sow_keyword_{pid}.json"


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_sections_store(pid: str) -> Dict:
    return _load_json(_sections_path(pid), {"active": [], "archived": [], "template_id": None})


def _save_sections_store(store: Dict, pid: str) -> None:
    _save_json(_sections_path(pid), store)


def _load_approved_store(pid: str) -> Dict:
    return _load_json(_approved_path(pid), {"sections": {}})


def _save_approved_store(store: Dict, pid: str) -> None:
    _save_json(_approved_path(pid), store)


def _load_keyword_store(pid: str) -> Dict:
    return _load_json(_keyword_path(pid), {})


def _save_keyword_store(store: Dict, pid: str) -> None:
    _save_json(_keyword_path(pid), store)


def _attachments_path(pid: str) -> Path:
    return _THIS_DIR / f"sow_attachments_{pid}.json"


def _load_attachments_store(pid: str) -> Dict:
    return _load_json(_attachments_path(pid), {"attachments": {}})


def _save_attachments_store(store: Dict, pid: str) -> None:
    _save_json(_attachments_path(pid), store)


# ─────────────────────────────────────────────────────────────────────────────
# Seeding — called by sow_template_routes.py when a template (or the fallback
# structure) is applied to a project. Public so that module can import it.
# ─────────────────────────────────────────────────────────────────────────────

def seed_project_sections(project_id: str, sections: List[Dict], template_id: Optional[str] = None,
                          engagement_type: Optional[str] = None) -> None:
    active = [
        {
            "id": s["id"], "title": s["title"], "level": s.get("level", 1),
            # Default True (generatable) when a template doesn't specify —
            # e.g. FALLBACK_SECTIONS is flat (no children), so this only
            # matters for a real template's has_own_content detection.
            "has_own_content": s.get("has_own_content", True),
        }
        for s in sections
    ]
    # Stamp the engagement template's version alongside the key so a draft can
    # always be traced back to the exact template revision that produced it
    # (see sow_engagement_templates' "version-controlled" note).
    from app.sow_engagement_templates import get_engagement_template
    eng_tpl = get_engagement_template(engagement_type)
    _save_sections_store({
        "active": active, "archived": [], "template_id": template_id,
        "engagement_type": engagement_type,
        "engagement_template_version": eng_tpl["version"] if eng_tpl else None,
    }, project_id)
    kw = _load_keyword_store(project_id)
    for s in sections:
        sid = s["id"]
        if sid not in kw:
            kw[sid] = {"status": "not-started", "custom_instructions": ""}
    _save_keyword_store(kw, project_id)


def get_project_template_id(project_id: str) -> Optional[str]:
    return _load_sections_store(project_id).get("template_id")


def get_project_engagement_type(project_id: str) -> Optional[str]:
    """The engagement type chosen for this project, or None for a project
    seeded before US-01 (or from a custom template with no type chosen).
    None means engagement-neutral guidance — the pre-US-01 behaviour."""
    return _load_sections_store(project_id).get("engagement_type")


def _ensure_seeded(project_id: str) -> None:
    """Auto-bootstrap a project's section list the first time it's needed, so
    a user who skips the explicit template-selection step isn't stuck with an
    empty project: prefer the org default template, else the built-in
    fallback structure transcribed from SOW_SKILL.md."""
    if _sections_path(project_id).exists():
        return
    default = get_default_template()
    if default and default.get("sections"):
        seed_project_sections(project_id, default["sections"], template_id=default["id"])
    else:
        seed_project_sections(project_id, FALLBACK_SECTIONS, template_id=None)


def _is_top_level(section_id: str) -> bool:
    return "." not in section_id


def _family_ids(parent_id: str, active_sections: List[Dict]) -> List[str]:
    """[parent_id, child1, child2, ...] in document order (active_sections is
    already in doc order from the template's own TOC extraction). Degenerates
    to [parent_id] alone for any leaf section (the common case — no id in
    the template starts with "{parent_id}.") — identical behavior to today's
    single-section flow for every such id.

    Only the sidebar surfaces top-level ids now (see list_sow_sections); a
    parent with children is drafted as one combined unit covering itself
    (if it has_own_content — see _draftable_blocks) plus every descendant."""
    return [s["id"] for s in active_sections
            if s["id"] == parent_id or s["id"].startswith(parent_id + ".")]


def _draftable_blocks(parent_id: str, active_sections: List[Dict]) -> List[str]:
    """The family ids that should actually receive drafted content: the whole
    family, EXCEPT the parent's own id when it's a pure container — a heading
    with children but no body of its own in the template (e.g. "3. Project
    Overview" jumps straight to "3.1 Background"). Mirrors the old
    _container_ids exclusion semantics, just applied within one family
    instead of blocking generation outright."""
    family = _family_ids(parent_id, active_sections)
    if len(family) == 1:
        return family
    sec = next((s for s in active_sections if s["id"] == parent_id), None)
    has_parent_content = sec.get("has_own_content", True) if sec else True
    return family if has_parent_content else family[1:]


# ─────────────────────────────────────────────────────────────────────────────
# Section drafting guidance — condensed from SOW_SKILL.md Section 4, Step 3.
# Keyed by the built-in fallback/default template's section ids ("1".."22" etc).
# A custom-uploaded template's own section ids won't match this dict — those
# sections fall back to generic guidance built from just the section title,
# which is still enough context together with the source material and the
# skill file's operating principles.
# ─────────────────────────────────────────────────────────────────────────────
SECTION_GUIDANCE: Dict[str, str] = {
    "cover": "Cover page: Title 'STATEMENT OF WORK', subtitle = confirmed Project Name. "
             "Field/Details table: Client Name, Project Name, Engagement Type, Contract ID, "
             "Effective Date, End Date, Document Version (start at v1.0), Prepared by "
             "(Bristlecone Inc.), Review Status (Draft).",
    "1": "Agreement Details — one sentence naming the client and Bristlecone Incorporated "
         "under the governing MSA/ISA/Consulting Agreement. Then a separate Field/Details "
         "table: Contract/SOW ID, SOW Effective Date, SOW End Date, Governing Agreement, "
         "Amendment/Supersedes, Agreement Duration, Delivery Location.",
    "2": "Executive Summary — one paragraph naming the engagement, three primary-objective "
         "bullets, and a closing sentence pointing to the Key Assumptions and Key "
         "Observations sections for deferred decisions.",
    "3": "Project Overview — Background (client's current state, pain points, business "
         "drivers); Objectives (bullets); Indicative To-Be Process Flow (high-level "
         "target-state description — include a process-flow diagram if one is available "
         "from the source material rather than describing it in prose alone).",
    "4": "Scope of Work — Geographical/Process/Technical/Services/Application Scope; "
         "Integration Scope as a table (# | Integration | Source System | Target System | "
         "Notes). Every scope statement must be specific to THIS engagement — name the "
         "client's actual systems, sites, processes and volumes from the source material. "
         "A sentence that would read identically on any other client's SOW is not scope, "
         "it is boilerplate: cut it or replace it with the specific fact. Where a boundary "
         "is genuinely undecided, emit a <CONFIRM: …> placeholder rather than a vague "
         "generality.",
    "5": "Proposed Architecture — solution architecture, deployment model, change-governance "
         "structure. Include an architecture diagram here if one is available from the "
         "source material.",
    "6": "Solution Implementation Approach & Project Plan — Implementation Methodology "
         "(include a relevant implementation/rollout diagram if available); Project "
         "Phases/Releases as a table (Phase/Release | Scope/Modules | Regions | Indicative "
         "Timeline | Status).",
    "7": "Project Management — Governance Model; Roles & Responsibilities as a table (Role | "
         "Party | Location | Responsibilities); Work Schedule; Escalation Matrix; Key "
         "Contacts. Governance is only real if a reader can tell WHO meets, HOW OFTEN, and "
         "WHO to call when something goes wrong — cover all three.",
    "8": "Personnel Requirements — bullets on language, background, domain familiarity, "
         "file-format/standards knowledge, methodology experience, certifications.",
    "9": "Key Deliverables — table (# | Deliverable | Description | Responsible Party | Due "
         "Date | Acceptance Criteria). Only list deliverables explicitly described in the "
         "source; mark unconfirmed dates with a placeholder rather than 'TBD'. Every "
         "deliverable must be specific to this engagement — a named artefact whose content "
         "is described, not a generic category like \"documentation\" or \"training\".",
    "10": "Key Assumptions — bullets, each one specific to this engagement and testable "
          "(a reader must be able to tell whether it held). Name the actual systems, teams, "
          "environments, data or dates the assumption depends on. Generic filler such as "
          "\"the client will be cooperative\" or \"resources will be available\" is not an "
          "assumption — either make it concrete or drop it. State explicitly that if an "
          "assumption does not hold, the Change Management Process applies and cost/timeline "
          "may be revised.",
    "11": "Acceptance Criteria — table (Deliverable/Milestone | Acceptance Criteria | "
          "Sign-off Party | Review Window). Three things are mandatory. (1) Criteria must be "
          "MEASURABLE per deliverable or milestone — a named test, threshold, count or "
          "document state a reviewer can check objectively; never 'client satisfaction', "
          "'high quality' or 'works as expected'. (2) The Review Window must be an explicit "
          "number of days and must say whether they are BUSINESS or CALENDAR days "
          "(e.g. 'ten (10) business days from submission'). (3) Immediately after the table, "
          "state the deemed-acceptance rule in its own sentence: if the client does not issue "
          "a written rejection identifying the specific failed criterion within the review "
          "window, the deliverable is deemed accepted automatically on expiry of that window. "
          "Also state the re-submission cycle for a rejected deliverable.",
    "12": "Obligations — Bristlecone Obligations (bullets); Client Obligations (bullets); "
          "Subcontractor & Vendor Alignment; Contractor & Vendor Compliance.",
    "13": "Commercials — Engagement Model; Rate Card & Pyramid Structure as a table "
          "(Role/Band | Location | Rate USD/day | Rate Type | Effective Date | Notes); Shift "
          "Allowance; Travel & Expenses; Invoicing & Payment Terms.",
    "14": "Performance Reporting & KPIs — table (Metric | Definition | Target | Measurement "
          "Frequency | Measurement Method | Data Source | Owner). Every metric must be "
          "MEASURABLE with a numeric or clearly binary target — never \"good performance\" "
          "or \"timely delivery\". Every row must say HOW it is measured and from WHICH "
          "system or artefact the data comes; a metric with no data source cannot be "
          "reported. Follow the table with a reporting table (Report | Frequency | "
          "Recipients). Then state the consequence of an SLA breach (service credits, "
          "remediation plan, escalation per the Escalation Matrix) — or, if this engagement "
          "carries no service credits, say that explicitly: \"No service credits or "
          "financial penalties apply to this engagement.\" Silence on breach consequences is "
          "not acceptable.",
    "15": "Risks — table (# | Risk Description | Likelihood | Impact | Mitigation | Risk "
          "Owner). Likelihood and Impact must each be a rating (High/Medium/Low) — never "
          "blank. Every risk needs a mitigation approach that names a concrete action, not "
          "\"monitor closely\". Every risk needs a named Risk Owner giving BOTH the party "
          "and the role (e.g. \"Bristlecone — Engagement Manager\" or \"Customer — IT "
          "Lead\"); a risk owned by nobody is not managed. Start from standard risks "
          "(deferred scope decisions, custom code/integration overrun, key SME "
          "unavailability, non-objective acceptance criteria) and add any "
          "engagement-specific risk the source material surfaces.",
    "16": "Governance & Risk Mitigation — Milestone Deliverables Matrix; Deferred Scope "
          "Decisions (bullets); Integration & Custom Code Safeguards; Change Control. Where "
          "this section discusses escalation, refer to the Escalation Matrix rather than "
          "restating different response times.",
    "17": "Change Management Process — the formal Change Request route for any scope "
          "deviation. Required content, all four parts. (1) The workflow as a table "
          "(Step | Activity | Owner | Turnaround), using the six-step flow: CR raised → CR "
          "logged → impact assessment → CR reviewed/approved → SOW amendment/addendum → CR "
          "closure. Every step needs a named owning role AND an explicit turnaround time in "
          "business days. (2) Name the approvers on BOTH sides by role (e.g. Client Project "
          "Sponsor and Bristlecone Engagement Manager) and say who holds final authority. "
          "(3) State that a commercial AND timeline impact assessment is MANDATORY for every "
          "CR without exception — a CR cannot be approved without both, and the assessment "
          "must quantify cost impact and schedule impact even when the answer is nil. "
          "(4) State plainly that no work on a change begins until the CR is approved and "
          "signed by both parties, and that unapproved work is not chargeable and does not "
          "extend any agreed date.",
    "18": "Security & Data Protection — bullets (patching/AV, encryption in transit/at rest, "
          "least privilege, information-security-policy compliance, incident reporting "
          "window, regulatory compliance e.g. GDPR/SOC2/ISO 27001).",
    "19": "Termination — Termination for Convenience (notice period); Termination for Cause "
          "(breach/insolvency/fraud grounds); Effect of Termination.",
    "20": "Acceptance and Sign-Off — table (blank rows) with columns Bristlecone Inc. | "
          "Client, rows Authorized Signatory / Name (Print) / Title / Date / Signature. "
          "Leave signature fields blank — never fabricate a name.",
    "21": "Key Observations — factual, non-opinion bullets about this draft's own content: "
          "deferred scope decisions, whether dated deliverables were specified, the "
          "integration count, and whether acceptance criteria/payment are linked at the "
          "phase or deliverable level.",
    "22": "Reviewer Recommendations — standard Governance & Risk Mitigation and Acceptance & "
          "Quality recommendations. For the Overall Recommendation, always use a "
          "'<CONFIRM: ...>' placeholder — a freshly generated draft should never rate its "
          "own signability.",
    "appA": "Appendix A — Technical Prerequisites, populated from the source where possible, "
            "placeholders otherwise.",
    "appB": "Appendix B — Resource Profiles & Certifications, populated from the source where "
            "possible, placeholders otherwise.",
    # Granular entries for the real branded template, which decomposes several
    # of the above into numbered sub-sections (e.g. "3.1 Background" rather
    # than one "3. Project Overview" section). generate_sow_section() also
    # falls back to the parent id's guidance above when a sub-id isn't listed
    # here, so this only needs entries where the sub-section needs guidance
    # more specific than its parent's.
    "0": "Cover page — Field/Details table only (Client Name, Project Name, Engagement Type, "
         "Contract ID, Effective Date, End Date, Document Version starting at v1.0, Prepared by "
         "'Bristlecone Inc.', Review Status 'Draft'). The page title itself is already fixed in "
         "the template — draft only the table values.",
    "3.1": "Background — the client's current state, pain points, and business drivers for this "
           "engagement.",
    "3.2": "Objectives — bullets.",
    "3.3": "Indicative To-Be Process Flow — high-level target-state description. Include a "
           "process-flow diagram here if one is available from the source material.",
    "4.1": "Geographical Scope — regions/countries in scope.",
    "4.2": "Process Scope — business processes in scope.",
    "4.3": "Technical Scope — technical components/developments/integrations in scope.",
    "4.4": "Services Scope — services included (program management, solution design, delivery, "
           "testing, deployment, hypercare, OCM, etc. as applicable).",
    "4.5": "Application Scope — application functionalities in scope.",
    "4.6": "Integration Scope as a table (# | Integration | Source System | Target System | Notes).",
    "4.7": "Exclusions (Out of Scope) — high-priority. Bullets, each stating explicitly what "
           "Bristlecone will NOT do, using plain negative wording (\"X is not included\", "
           "\"no Y will be provided\", \"excludes Z\"). Name the specific thing excluded — "
           "systems, environments, geographies, data migration, training, licences, "
           "third-party costs, post-go-live support beyond the stated hypercare window. "
           "Do not describe what IS in scope here, and never leave this section as a "
           "placeholder alone if the source material hints at any boundary. Close with the "
           "catch-all: anything not expressly stated as in scope in this SOW is excluded "
           "and requires a change request.",
    "4.8": "Dependencies — table (# | Dependency | Owning Party | Needed By | Impact if Late). "
           "EVERY row must name the owning party in the Owning Party column, and it must be "
           "exactly one of: Customer, Bristlecone, or a named third party (e.g. \"Third "
           "party — SAP\"). A dependency with no named owner is not a dependency, it is an "
           "assumption — either assign it or move it to Key Assumptions. Follow the table "
           "with one line stating that a missed dependency date is handled through the "
           "Change Management Process.",
    "6.1": "Implementation Methodology — delivery methodology. Include a relevant "
           "implementation/rollout diagram if the source material has one.",
    "6.2": "Project Phases/Releases as a table (Phase/Release | Scope/Modules | Regions | "
           "Indicative Timeline | Status).",
    "7.1": "Governance Model & Forums — table (Forum | Purpose | Cadence | Participants | "
           "Chair). List every governance body that will actually meet (e.g. daily stand-up, "
           "weekly project review, monthly steering committee), each with an explicit cadence "
           "(daily/weekly/fortnightly/monthly) and named participant ROLES from BOTH parties. "
           "A forum with no cadence or no named participants is not governance.",
    "7.2": "Roles & Responsibilities as a table (Role | Party | Location | Responsibilities).",
    "7.3": "Work Schedule — bullets.",
    "7.4": "Escalation Matrix — table (Level | Trigger | Client Role | Bristlecone Role | "
           "Response Time | Resolution Target). Exactly three levels, L1 through L3, each "
           "with a named ROLE on BOTH sides (never just \"management\") and an explicit "
           "response time in hours or business days. Say what escalates a matter to the next "
           "level (unresolved after the resolution target, or severity).",
    "7.5": "Key Contacts — table (Name | Role | Party | Email | Phone). Both parties must be "
           "represented. Use <CONFIRM: …> placeholders for details the source material does "
           "not give — never invent a name, email address or phone number. Add one line "
           "stating that contact details are maintained by both parties and updated in "
           "writing without needing a change request.",
    "8.1": "Onboarding & Access Provisioning — table (# | Onboarding Step | Owning Party | "
           "Expected Duration | Prerequisite). Cover the whole path to a billable engineer: "
           "background verification, client induction, laptop/VDI issue, network and VPN "
           "access, application accounts and roles, and any client-specific training. Every "
           "step needs an owning party and an expected duration in business days. Under the "
           "table, list the CUSTOMER obligations separately — VPN and remote-access "
           "provisioning, named application licences, physical site or badge access, and "
           "sponsor sign-off — each with the date or lead time it is needed by. Close with "
           "the delay consequence stated plainly: what happens to the schedule (milestone "
           "dates move by the elapsed delay) AND to billing (whether allocated resources are "
           "chargeable while blocked, or whether the delay is handled as a change request). "
           "This last sentence is the point of the section; never omit it.",
    "8.2": "Asset Management & Return — table (Asset Type | Issued By | Issued To | Tracking "
           "Reference | Return Trigger | Return Timeline). Cover laptops, VDI or VPN tokens, "
           "access cards, mobile devices and named software licences. Describe how issuance "
           "is recorded and tracked (an asset register with a unique reference per item). "
           "State return obligations for BOTH triggers: project closure or termination, and "
           "individual resource rollover or replacement mid-engagement — each with a return "
           "timeline in business days. Finish with accountability for loss or non-return: who "
           "bears the cost, how it is recovered (deduction from final settlement or invoice "
           "for replacement value), and who certifies that all assets are accounted for at "
           "closure.",
    "12.1": "Bristlecone Obligations — bullets.",
    "12.2": "Client Obligations — bullets.",
    "12.3": "Subcontractor & Vendor Alignment — back-to-back cover, so Bristlecone never owes "
            "the client more than it can pass through to a vendor. Table (Obligation | "
            "Customer SOW Commitment | Vendor Contract Term | Aligned?) covering scope, "
            "SLAs/KPIs, acceptance basis, liability caps and payment terms. Three rules must "
            "be stated in words: (1) vendor scope, SLAs and acceptance terms MIRROR this SOW; "
            "(2) the vendor's acceptance basis matches this engagement model — where "
            "deliverables govern acceptance, vendor acceptance is deliverable-based and is "
            "NOT replaced by timesheet approval; (3) vendor payment terms are compatible with "
            "the customer credit period, i.e. the vendor is paid no sooner than the client "
            "pays plus a stated buffer, so Bristlecone does not fund the gap. Flag any "
            "misalignment explicitly rather than leaving a row blank.",
    "12.4": "Contractor & Vendor Compliance — all four of POSH (Prevention of Sexual "
            "Harassment), the Code of Conduct, ISMS (information security management) and "
            "PIMS (privacy information management) are CONTRACTUALLY BINDING on every vendor "
            "and on each of their personnel, flowed down through the vendor contract. Name "
            "all four explicitly. State that documented evidence of acknowledgement or "
            "training completion is required from each individual BEFORE any system, data or "
            "site access is granted, and name who verifies it. State the consequences of "
            "non-compliance (access revocation, removal of the individual from the "
            "engagement, and termination of the vendor contract for material breach) and "
            "Bristlecone's and the client's audit rights over vendor compliance records, "
            "with the notice period for an audit.",
    "13.1": "Engagement Model — Fixed Price / Time & Material / Fixed Monthly / AMS retainer.",
    "13.2": "Rate Card & Pyramid Structure as a table (Role/Band | Location | Rate USD/day | Rate "
            "Type | Effective Date | Notes).",
    "13.3": "Shift Allowance — bullets.",
    "13.4": "Travel & Expenses — reimbursement terms.",
    "13.5": "Invoicing & Payment Terms.",
    "14.1": "SLAs, Targets & Breach Consequences — table (SLA | Target | Measurement Method | "
            "Data Source | Reporting Frequency). Targets must be numeric or clearly binary, "
            "and every row must say how it is measured and from which system or artefact the "
            "data comes. Immediately after the table, state the consequence of a breach — "
            "service credits with their calculation, a remediation plan with a deadline, or "
            "escalation per the Escalation Matrix. If this engagement carries no service "
            "credits or financial penalties (e.g. an implementation-only engagement), say so "
            "in an explicit sentence — do not leave it silent.",
    "16.1": "Milestone Deliverables Matrix — reference the Key Deliverables and Commercials "
            "sections.",
    "16.2": "Deferred Scope Decisions — bullets.",
    "16.3": "Integration & Custom Code Safeguards — bullets.",
    "16.4": "Change Control — reference the Change Management Process section.",
    "19.1": "Termination for Convenience — notice period.",
    "19.2": "Termination for Cause — breach/insolvency/fraud grounds.",
    "19.3": "Effect of Termination.",
    "22.1": "Governance & Risk Mitigation recommendations — standard reviewer additions.",
    "22.2": "Acceptance & Quality recommendations — standard reviewer additions.",
    "22.3": "Overall Recommendation — always use a '<CONFIRM: ...>' placeholder; a freshly "
            "generated draft should never rate its own signability.",
}


def _guidance_for_section(section_id: str, engagement_type: Optional[str] = None,
                          tpl_guidance: Optional[Dict[str, str]] = None,
                          tpl_strict: bool = False) -> str:
    """Exact match first, else fall back to the parent top-level id (e.g.
    '3.1' -> '3') so a granular real-template sub-section still gets sensible
    guidance even though SECTION_GUIDANCE's detailed entries are keyed mostly
    by the fallback template's top-level ids.

    When the project has an engagement type (US-01), that template's guidance
    wins outright for the sections it covers — Commercials, Acceptance
    Criteria and Key Deliverables read very differently on a T&M engagement
    than on a Fixed Cost one, and a merge of the two would produce a section
    that describes both billing models at once. Everything the engagement
    template says nothing about falls through to the shared guidance below,
    so only the commercially-sensitive sections diverge."""
    override = engagement_guidance(engagement_type, section_id)
    if override:
        return override

    # A library template may carry guidance keyed by its OWN numbering. It
    # wins over the built-in map, because the built-in map is keyed by a
    # different document's numbering and would otherwise describe a
    # different subject entirely (see get_template_guidance).
    if tpl_guidance:
        if section_id in tpl_guidance:
            return tpl_guidance[section_id]
        parent = section_id.split(".")[0]
        if parent in tpl_guidance:
            return tpl_guidance[parent]

    # `tpl_strict` says this template's numbering is known NOT to line up with
    # the built-in numbering, so silence is safer than confidently wrong
    # guidance — the section's own title still reaches the prompt.
    if tpl_strict:
        return ""

    if section_id in SECTION_GUIDANCE:
        return SECTION_GUIDANCE[section_id]
    parent = section_id.split(".")[0]
    return SECTION_GUIDANCE.get(parent, "")


def _table_ids_for(engagement_type: Optional[str]) -> Set[str]:
    """The shared table sections plus any the engagement template adds (a
    T&M rate card, a Fixed Cost milestone payment schedule)."""
    return TABLE_SECTION_IDS | engagement_table_ids(engagement_type)


# ─────────────────────────────────────────────────────────────────────────────
# Combined-family drafting: when a top-level section has sub-sections (e.g.
# "3" has "3.1"/"3.2"/"3.3"), one generate call drafts all of them together
# in a single AI response, marker-delimited so the review/approve step can
# split it back onto each sub-section's own id. Marking each block with its
# id (not just its title) means the split is a simple, unambiguous regex
# match — immune to the model paraphrasing or re-casing a title.
# ─────────────────────────────────────────────────────────────────────────────
_MARKER_RE = re.compile(r'^[ \t]{0,3}#{1,4}[ \t]*([\d]+(?:\.[\d]+)*)\b.*$', re.MULTILINE)


def _marker_line(block_id: str, title: str) -> str:
    return f"### {block_id} {title}"


def _split_combined_content(text: str, block_ids: List[str]) -> Dict[str, str]:
    """Split one combined, marker-delimited AI response back into per-id
    content. Only markers whose id is in `block_ids` are recognized, so a
    stray '#'-prefixed line elsewhere in the text (or a marker belonging to a
    different family) can't be mistaken for a real split point."""
    matches = [(m.start(), m.end(), m.group(1)) for m in _MARKER_RE.finditer(text)
               if m.group(1) in block_ids]
    result: Dict[str, str] = {}
    for i, (start, end, bid) in enumerate(matches):
        content_end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        result[bid] = text[end:content_end].strip()
    return result


# Sections/sub-sections where a diagram or image would meaningfully help —
# unions the existing hand-picked seed list with every SECTION_GUIDANCE entry
# that already textually mentions "diagram", so it's self-maintaining as
# guidance text evolves rather than a second hand-maintained list. Used to
# badge the sidebar ("suggest which sections would benefit from an image").
IMAGE_SUGGESTED_SECTION_IDS = set(DIAGRAM_PRIORITY_SECTION_IDS) | {
    sid for sid, guidance in SECTION_GUIDANCE.items() if "diagram" in guidance.lower()
}


# Subject matter that a figure genuinely helps explain. Matched against a
# section's own title and guidance text, so this works for any SOW template.
#
# DIAGRAM_PRIORITY_SECTION_IDS lists bare section numbers ("3", "5", "6",
# "3.3", "6.1") from the fallback template. Those numbers mean nothing in a
# client's own template — section 3 might be Commercials and the architecture
# section might be 9 — so relying on them alone forced figures into whichever
# sections happened to carry those numbers while leaving the real
# architecture and process-flow sections treated as prose. The id set is kept
# as an additional signal for the fallback template rather than the only one.
_DIAGRAM_TITLE_WORDS = re.compile(
    r"\b("
    r"architecture|landscape|topology|"
    r"process\s+flow|to-?be|as-?is|workflow|swimlane|"
    r"integration|interface|data\s+flow|"
    r"solution\s+design|system\s+design|technical\s+design|"
    r"methodology|approach|framework|operating\s+model|"
    r"governance\s+model|delivery\s+model|engagement\s+model|"
    r"roadmap|timeline|phases|releases|milestones|"
    r"org(?:anisation|anization)?\s+chart|raci|"
    r"scope\s+overview|solution\s+overview"
    r")\b", re.I)


def _section_wants_diagram(section_ids, sec_lookup=None, extra_text: str = "") -> bool:
    """Whether a figure is expected in this section, judged from its subject.

    The section's own title is the authority whenever one is known, because it
    is the only signal that means the same thing in every template. Both
    id-keyed sources — DIAGRAM_PRIORITY_SECTION_IDS and SECTION_GUIDANCE — are
    keyed to the *fallback* template's numbering, so consulting them for a
    client template whose numbering differs reads another section's meaning
    entirely: it marked "Commercial Terms", "Termination for Convenience" and
    "Key Assumptions" as diagram-worthy purely because the fallback template
    happens to describe diagrams at those same numbers. They are therefore
    used only when no title is available to judge from.
    """
    ids = [section_ids] if isinstance(section_ids, str) else list(section_ids or [])

    titles = [extra_text] if extra_text else []
    if sec_lookup:
        for sid in ids:
            entry = sec_lookup.get(sid) or {}
            if isinstance(entry, dict) and entry.get("title"):
                titles.append(entry["title"])

    if titles:
        joined = " ".join(titles)
        return bool(_DIAGRAM_TITLE_WORDS.search(joined)) or "diagram" in joined.lower()

    # No title to go on — fall back to the fallback template's own id-keyed
    # signals, which are correct for projects actually using that template.
    # Only an explicit mention of a diagram counts here: _DIAGRAM_TITLE_WORDS
    # is tuned for terse titles, and against paragraphs of guidance prose
    # incidental words like "approach" match almost everything.
    for sid in ids:
        if sid in DIAGRAM_PRIORITY_SECTION_IDS:
            return True
        if "diagram" in _guidance_for_section(sid).lower():
            return True
    return False


NO_LEAK_RULES = """Hard rules for this draft:
- Never mention "RFP", an internal sourcing dashboard, or any Bristlecone-internal artifact name.
- Never cite a slide number, page number, or internal document locator inline in the drafted
  text — such references are for your own drafting reference only, never for client-facing output.
- Write only in prose/bullets UNLESS this section is explicitly flagged below as a table
  section — then use the table shape described. Do not introduce a table anywhere else."""

PLACEHOLDER_RULES = """Placeholder convention — anything not confirmed by the source material or
the already-approved sections below gets a placeholder in this exact format (write it plain, with
no surrounding backticks or other Markdown code-span formatting), so a human can find every open
item with one search for the < character:
- <CONFIRM: description of what's needed> — for facts/decisions
- <TECH TEAM: description> — for technical detail outside this skill's competence
- <LEGAL: description> — for anything touching MSA-governed terms
Never fill these with a plausible-sounding guess instead of a placeholder.
Use at most 1-2 placeholders for the WHOLE section. Only tag something as unresolved when it is
genuinely absent from both the source material and common sense — do not hedge a fact the source
material already gives you, and do not manufacture a separate placeholder for every minor detail
of something already confirmed at a higher level."""

DENSITY_RULES = """Density and length — the most important instruction in this prompt. A real,
professionally-authored SOW is terse and direct, not padded corporate prose. Match that exactly:
- State each fact once. Never restate the same point in a second or third sentence "for clarity"
  or "to emphasize."
- No filler sentences: cut "It should be noted that...", "This section is intended to...", "The
  parties acknowledge that...", "Subject to the foregoing..." — state the substantive fact
  directly instead of wrapping it in throat-clearing.
- A typical prose subsection runs 80-200 words total. Only exceed that if the guidance above
  genuinely lists multiple distinct named sub-topics that each need their own line(s).
- Bullets are single lines each — a bullet is not an invitation to write a paragraph.
- If you notice you are about to write a second sentence that means the same thing as the one
  before it, delete it instead."""

# Defense-in-depth: strip any locator pattern that slips through despite the
# prompt rules above, before content ever reaches the reviewer or an export.
_LOCATOR_PATTERNS = [
    re.compile(r"\bSlide\s+\d+\b\.?", re.IGNORECASE),
    re.compile(r"\[Source:[^\]]*\]"),
    re.compile(r"\bRFP\s+dashboard\b", re.IGNORECASE),
    re.compile(r"\(\s*(?:page|pg\.?)\s*\d+\s*\)", re.IGNORECASE),
    re.compile(r"\bon\s+(?:page|pg\.?)\s+\d+\b", re.IGNORECASE),
]


def _sanitize_locators(text: str) -> str:
    out = text or ""
    for pat in _LOCATOR_PATTERNS:
        out = pat.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _tokenize(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


# Chunk text lives in the "chunk_text" field (see chunking_pipeline.make_chunk) —
# NOT "text". Centralized here so every place that reads chunk content agrees.
def _chunk_text(chunk: Dict) -> str:
    return chunk.get("chunk_text") or chunk.get("text") or ""


def _score_chunk(chunk: Dict, query_tokens: set) -> float:
    text = f"{_chunk_text(chunk)} {chunk.get('section_heading', '')} {chunk.get('doc_name', '')}"
    tokens = _tokenize(text)
    if not tokens or not query_tokens:
        return 0.0
    overlap = len(tokens & query_tokens)
    return overlap / (len(query_tokens) ** 0.5)


def _keyword_search_chunks(chunks: List[Dict], query: str, top_k: int = 6) -> List[Dict]:
    q_tokens = _tokenize(query)
    scored = [(_score_chunk(c, q_tokens), c) for c in chunks]
    scored = [(s, c) for s, c in scored if s > 0]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]


def _search_chunks(chunks: List[Dict], query: str, top_k: int = 6) -> List[Dict]:
    """Hybrid retrieval: semantic (embedding) results blended with keyword
    overlap results, falling back to keyword alone when the project's chunks
    have no embeddings (chunking_pipeline embeds every chunk automatically
    whenever OPENAI_API_KEY is set, regardless of the SEARCH_BACKEND flag that
    gates BRD's own retrieval) or the embedding call itself fails.

    Previously this was strictly either/or: if semantic search returned ANY
    hit above its threshold, keyword search was never consulted. That silently
    lost chunks whose wording matches the query exactly but whose overall topic
    does not — the common shape for short factual lines (a date, an ID, a
    location) sitting on a slide that is mostly about something else. Reserving
    a slice of the budget for keyword hits keeps those recoverable while
    leaving semantic ranking in charge of the majority of the window.
    """
    if not chunks or not query.strip():
        return []

    semantic_hits: List[Dict] = []
    if any(c.get("embedding") for c in chunks):
        try:
            from app.semantic_search import semantic_search
            semantic_hits = [c for _, c in
                             semantic_search(query, chunks, top_k=top_k, min_score=0.2)]
        except Exception as exc:
            logger.warning("Semantic search failed, falling back to keyword search: %s", exc)

    if not semantic_hits:
        return _keyword_search_chunks(chunks, query, top_k=top_k)

    # Keep at least one slot (a quarter of the window) for keyword-only hits.
    keyword_slots = max(1, top_k // 4)
    merged = semantic_hits[: top_k - keyword_slots]

    def _identity(c: Dict) -> str:
        # chunk_id is an int in some pipelines and a str in others — coerce so
        # the two never hash as different chunks.
        cid = c.get("chunk_id")
        return f"id:{cid}" if cid is not None else _chunk_text(c)[:120]

    seen = {_identity(c) for c in merged}
    for c in _keyword_search_chunks(chunks, query, top_k=top_k):
        if len(merged) >= top_k:
            break
        if _identity(c) not in seen:
            seen.add(_identity(c))
            merged.append(c)

    # Backfill from semantic if keyword produced too few distinct extras.
    for c in semantic_hits:
        if len(merged) >= top_k:
            break
        if _identity(c) not in seen:
            seen.add(_identity(c))
            merged.append(c)

    return merged


def _load_all_project_chunks(project_id: str) -> List[Dict]:
    from app.project_routes import PROJECTS, UPLOAD_ROOT
    from app.chunking_pipeline import load_project_chunks

    if project_id not in PROJECTS:
        return []
    chunks = load_project_chunks(UPLOAD_ROOT / project_id)
    repo_ids = PROJECTS[project_id].get("repositories", [])
    if repo_ids:
        from app.repository_routes import load_chunks_for_repos
        chunks = chunks + load_chunks_for_repos(repo_ids)
    return chunks


def _project_source_doc_names(project_id: str) -> set:
    """Filenames this project can legitimately pull images from — its own
    uploads plus any attached repository's files. The underlying image index
    (image_index.py) is global across every project, so this scoping happens
    here rather than in that shared module, to avoid touching BRD's working
    image-retrieval path."""
    from app.project_routes import PROJECTS, FILES

    names = {f["name"] for f in FILES.values() if f["project_id"] == project_id}
    repo_ids = PROJECTS.get(project_id, {}).get("repositories", [])
    if repo_ids:
        from app.repository_routes import REPO_FILES
        names |= {f["name"] for f in REPO_FILES.values() if f.get("repo_id") in repo_ids}
    return names


_IMAGE_TAG_ID_RE = re.compile(r'<IMAGE id="([^"]+)"\s*/?>')


def _used_image_ids(project_id: str) -> set:
    """Image ids already embedded in this project's drafted/approved sections.

    Read fresh from the section stores on each call rather than cached, since
    sections are drafted one HTTP request at a time and the set grows as the
    document is built up.
    """
    used: set = set()
    try:
        for store, key in ((_load_approved_store(project_id), "sections"),
                           (_load_sections_store(project_id), "active")):
            blob = store.get(key)
            entries = blob.values() if isinstance(blob, dict) else (blob or [])
            for entry in entries:
                if isinstance(entry, dict):
                    text = entry.get("content") or ""
                else:
                    text = str(entry or "")
                used.update(_IMAGE_TAG_ID_RE.findall(text))
    except Exception as e:
        logger.debug("Could not read used image ids for %s: %s", project_id, e)
    return used


def _relevant_images_for_section(project_id: str, query: str, section_ids,
                                 section_title: str = "") -> List[Dict]:
    """`section_ids` may be a single id (str, back-compat) or a list — a
    combined family generate call biases top_k using ANY member's presence
    in DIAGRAM_PRIORITY_SECTION_IDS, since the diagram-worthy sub-section is
    often a child (e.g. "3.3") rather than the parent itself."""
    ids = [section_ids] if isinstance(section_ids, str) else list(section_ids)
    doc_names = _project_source_doc_names(project_id)
    if not doc_names:
        return []
    # Retrieve a wider candidate set than we intend to offer. The relevance
    # verifier downstream removes most of it, and starting from top_k=2 left
    # nothing to choose from once the wrong ones were dropped.
    top_k = 8 if _section_wants_diagram(ids, extra_text=section_title) else 5
    # The project filter goes INTO the search, not after it. Fetching a global
    # top-k and filtering the survivors starved this badly: the image index is
    # shared across every project (thousands of records), so on a real project
    # whose deck contributed 118 images, the global top-20 for a section query
    # contained none of them at all — sections were offered zero images and no
    # <IMAGE> tag was ever emitted. Also prefers real diagrams over the vendor
    # logos that otherwise match generic section vocabulary.
    candidates = get_relevant_images(
        query,
        top_k=top_k,
        source_docs=doc_names,
        prefer_diagrams=True,
    )
    # Skip anything an earlier section of this same document already used, so
    # this section falls through to its own next-best figure rather than
    # repeating one the reader has already seen.
    candidates = filter_already_used(candidates, _used_image_ids(project_id))
    if not candidates:
        return []
    # Keyword retrieval cannot distinguish a diagram of this section's subject
    # from a logo sitting on a slide that mentions it, so the shortlist is
    # confirmed against what the section is actually about before any of it is
    # offered to the generator for embedding.
    return verify_image_relevance(
        candidates,
        section_title=section_title or ", ".join(str(i) for i in ids),
        section_context=query,
    )


def _format_attachment_context(block_id: str, title: str, attachments: List[Dict]) -> str:
    """Reviewer-attached reference material for one block, formatted as
    PRIORITY context ahead of the generally-retrieved source material. Reuses
    the same <IMAGE id="..."/> embed-instruction convention
    format_images_for_context already uses, so an attached image flows
    through the exact same export-time resolver with no extra plumbing."""
    parts = [f'PRIORITY REFERENCE MATERIAL for "{title}" — provided directly by the '
             f'reviewer for this sub-part; prioritize this over the general source '
             f'material below.']
    for att in attachments:
        parts.append(f"  [{att.get('filename', '')}] {att.get('caption', '')}")
        if att.get("kind") == "document" and att.get("extracted_text"):
            parts.append(f"  {att['extracted_text'][:3000]}")
        if att.get("kind") == "image" and att.get("image_id"):
            parts.append(
                f'  EMBED INSTRUCTION: <IMAGE id="{att["image_id"]}"/> — this image is '
                f"attached directly to this sub-part; include it unless clearly irrelevant."
            )
        if att.get("kind") == "document" and att.get("image_ids"):
            for image_id in att["image_ids"]:
                parts.append(
                    f'  EMBED INSTRUCTION: <IMAGE id="{image_id}"/> — this image/flowchart '
                    f"was extracted from the attached document above; include it if relevant "
                    f"to this sub-part."
                )
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    project_id: str
    custom_instructions: str = ""


class ApproveRequest(BaseModel):
    project_id: str
    content: str


class BulkGenerateRequest(BaseModel):
    project_id: str
    section_ids: Optional[List[str]] = None


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sections")
def list_sow_sections(project_id: str):
    """Only top-level sections are surfaced here now — a sub-section (e.g.
    "3.1") is drafted together with its parent (see generate_sow_section) and
    is never an independently reviewable sidebar item. Children stay in the
    underlying sections_store for _family_ids/_draftable_blocks to use."""
    _ensure_seeded(project_id)
    sections_store = _load_sections_store(project_id)
    kw = _load_keyword_store(project_id)
    approved = _load_approved_store(project_id)
    active = sections_store.get("active", [])
    approved_sections = approved.get("sections", {})
    table_ids = _table_ids_for(sections_store.get("engagement_type"))

    out = []
    for s in active:
        sid = s["id"]
        if not _is_top_level(sid):
            continue
        entry = kw.get(sid, {})
        family = _family_ids(sid, active)
        blocks = _draftable_blocks(sid, active)
        has_content = bool(blocks) and all(bid in approved_sections for bid in blocks)
        if has_content:
            status = "generated"
        elif entry.get("draft_content"):
            status = "in-progress"
        else:
            status = "not-started"
        out.append({
            "id": sid,
            "title": s["title"],
            "level": s.get("level", 1),
            "status": status,
            "custom_instructions": entry.get("custom_instructions", ""),
            "is_table_section": any(bid in table_ids for bid in family),
            "has_content": has_content,
            "draft_content": entry.get("draft_content", ""),
            "draft_sources": entry.get("draft_sources", []),
            "child_count": len(family) - 1,
            # Judged from the section's subject matter, not its number, so the
            # hint is right for any template's numbering.
            "image_suggested": (
                any(bid in IMAGE_SUGGESTED_SECTION_IDS for bid in family)
                or _section_wants_diagram(family, extra_text=s["title"])
            ),
            # Every id (parent + children) this section drafts as one unit —
            # used by the attachment panel so the reviewer can target a
            # specific sub-part (e.g. "3.3 Indicative To-Be Process Flow")
            # rather than only the top-level id.
            "family": [{"id": bid, "title": next(x["title"] for x in active if x["id"] == bid)}
                       for bid in family],
        })
    return {
        "project_id": project_id,
        "template_id": sections_store.get("template_id"),
        "engagement_type": sections_store.get("engagement_type"),
        "engagement_template_version": sections_store.get("engagement_template_version"),
        "sections": out,
    }


@router.post("/sections/{section_id}/generate")
def generate_sow_section(section_id: str, req: GenerateRequest):
    if not SOW_SKILL_TEXT:
        raise HTTPException(500, "SOW_SKILL.md could not be loaded on the server")

    project_id = req.project_id
    _ensure_seeded(project_id)
    sections_store = _load_sections_store(project_id)
    active_sections = sections_store.get("active", [])
    sec = next((s for s in active_sections if s["id"] == section_id), None)
    if not sec:
        raise HTTPException(404, f"Section '{section_id}' not found for this project")
    if not _is_top_level(section_id):
        raise HTTPException(
            400,
            "Sub-sections are drafted together with their parent — generate the parent "
            "section instead.",
        )

    sec_lookup = {s["id"]: s for s in active_sections}
    blocks = _draftable_blocks(section_id, active_sections)

    # US-01: the engagement type chosen at the start of drafting decides which
    # commercial/acceptance language this section gets. None (a pre-US-01
    # project) keeps the engagement-neutral guidance.
    engagement_type = sections_store.get("engagement_type")
    table_ids = _table_ids_for(engagement_type)
    # A custom template's own guidance, keyed by its own section numbering.
    from app.sow_template_routes import get_template_guidance
    tpl_guidance, tpl_strict = get_template_guidance(sections_store.get("template_id"))

    from app.project_routes import PROJECTS
    client_name = PROJECTS.get(project_id, {}).get("client", "") or ""

    approved = _load_approved_store(project_id)
    approved_sections = approved.get("sections", {})
    attachments_store = _load_attachments_store(project_id)
    block_attachments = attachments_store.get("attachments", {})

    # Cross-section context: every already-approved section that comes before
    # this one in the section order, so this draft stays consistent with what
    # was already committed (mirrors screen3_routes.py's approach for BRD).
    context_parts = []
    for s in active_sections:
        if s["id"] == section_id:
            break
        prior = approved_sections.get(s["id"])
        if prior:
            context_parts.append(f"### {s['title']} (already approved)\n{prior['content']}")
    prior_context = "\n\n".join(context_parts)

    is_combined = len(blocks) > 1
    is_table = any(bid in table_ids for bid in blocks)

    # Retrieval query aggregates every block's own title+guidance vocabulary,
    # not just the (possibly container) parent's bare title — a combined
    # family needs recall across every sub-topic it's about to draft.
    query = " ".join(f"{sec_lookup[b]['title']} {_guidance_for_section(b, engagement_type, tpl_guidance, tpl_strict)}" for b in blocks)
    query = f"{query} {req.custom_instructions}".strip()

    # Separate, title-only query for image retrieval. Stored sections carry no
    # guidance of their own, so _guidance_for_section falls back to the
    # DEFAULT template's text keyed by section number — on a client template
    # that numbers things differently, that is a different section's
    # vocabulary entirely, and feeding it to the image index retrieves figures
    # for a topic this section is not about. The section's own title and its
    # sub-section titles are the only description that is accurate in every
    # template, so image search uses those.
    image_query = " ".join(
        sec_lookup[b]["title"] for b in blocks if sec_lookup.get(b, {}).get("title")
    )
    image_query = f"{image_query} {req.custom_instructions}".strip()
    chunks = _load_all_project_chunks(project_id)
    matches = _search_chunks(chunks, query, top_k=6)

    # Project-wide fact sheet (dates, client, duration, locations …), extracted
    # once per project and cached. Per-section similarity search reliably
    # misses these — a line like "June 2026 kickoff to Feb 2028 Go-live" ranked
    # 50th of 60 for this section's own query on a real deck — so they are
    # injected directly rather than left to win a retrieval lottery. See
    # sow_key_facts.py for the full rationale.
    try:
        from app.sow_key_facts import extract_key_facts, format_facts_for_context
        key_facts = extract_key_facts(project_id, chunks, client_hint=client_name)
        facts_block = format_facts_for_context(key_facts)
    except Exception as exc:
        logger.warning("Key-fact extraction unavailable for %s: %s", project_id, exc)
        facts_block = ""
    # Cap must stay >= CHUNK_SIZE_CHARS — a lower cap here (previously a flat
    # 1500 against chunks that can be up to 2000 chars) silently truncated
    # exactly the kind of trailing table content (phase/date tables often
    # sit at the end of a slide's block) this section's guidance asks for.
    from app.chunking_pipeline import CHUNK_SIZE_CHARS
    _source_char_cap = CHUNK_SIZE_CHARS + 200
    source_block = "\n\n".join(
        f"[{c.get('doc_name', 'source')} — {c.get('section_heading', '')}]\n{_chunk_text(c)[:_source_char_cap]}"
        for c in matches
    )

    image_hits = _relevant_images_for_section(project_id, image_query, blocks,
                                              section_title=sec.get("title", ""))

    if is_combined:
        # One combined prompt drafts every sub-part in this family together,
        # each introduced by an unambiguous "### {id} {title}" marker line so
        # the approve step can split the reviewed/edited result back onto
        # each sub-part's own id (see _split_combined_content).
        block_instructions = []
        for i, bid in enumerate(blocks, 1):
            title = sec_lookup[bid]["title"]
            b_guidance = _guidance_for_section(bid, engagement_type, tpl_guidance, tpl_strict)
            marker = _marker_line(bid, title)
            line = (f'{i}. Introduce this sub-part with the exact line "{marker}", '
                    f"then draft: {b_guidance or title}")
            if bid in table_ids:
                line += " (use a Markdown table for this one)."
            prior = approved_sections.get(bid)
            if prior:
                line += (f"\n   Existing approved content for this sub-part (preserve unless "
                         f"the reviewer's instructions below say otherwise):\n   {prior['content']}")
            atts = block_attachments.get(bid) or []
            if atts:
                line += "\n   " + _format_attachment_context(bid, title, atts).replace("\n", "\n   ")
            block_instructions.append(line)
        section_heading_text = (
            f"SECTION TO DRAFT: {sec['title']} — this top-level section has {len(blocks)} "
            f"sub-part(s). Draft ALL of them together in this ONE response, in this exact "
            f"order. Before each sub-part's content, output a line in EXACTLY this format "
            f'(three "#" characters, one space, the sub-part\'s number, one space, its title '
            f'— nothing else on that line, and never use "#" anywhere else in your answer, '
            f"for any other purpose — use bold text instead of a heading if you need "
            f"emphasis):\n### {{id}} {{title}}\n\n" + "\n".join(block_instructions)
        )
    else:
        bid = blocks[0]
        guidance = _guidance_for_section(bid, engagement_type, tpl_guidance, tpl_strict)
        section_heading_text = f"SECTION TO DRAFT: {sec['title']}\n\n" + (
            f"Drafting guidance for this section: {guidance}" if guidance else ""
        )
        atts = block_attachments.get(bid) or []
        if atts:
            section_heading_text += "\n\n" + _format_attachment_context(bid, sec["title"], atts)

    # The engagement premise goes ABOVE the section guidance so it frames every
    # section, not just the ones with engagement-specific guidance of their
    # own: an Executive Summary or a Key Assumptions section that mentions
    # billing has to describe the same model as Commercials does. It is also
    # the only engagement signal a custom client template gets, since that
    # template's own section ids never match the guidance keys.
    engagement_block = engagement_premise(engagement_type)

    research_context = f"""{engagement_block}

{section_heading_text}

{NO_LEAK_RULES}
{"At least one sub-part of this section uses a table, per the shape described above." if is_table else "This section is prose/bullets — do not introduce a table."}

{DENSITY_RULES}

{PLACEHOLDER_RULES}

{('Client name: ' + client_name) if client_name else ''}

{facts_block}

{('Already-approved earlier sections (for consistency — do not contradict):' + chr(10) + prior_context) if prior_context else ''}

{('Relevant source material found for this section:' + chr(10) + source_block) if source_block else 'No specific source material was found for this section — use placeholders for anything not confirmed.'}

{('Additional instructions from the reviewer: ' + req.custom_instructions) if req.custom_instructions else ''}
"""
    if image_hits:
        # Sections whose subject matter calls for a figure must actually carry
        # one — see format_images_for_context's require_at_least_one note.
        # Decided from the section's title and guidance rather than its
        # number, so this holds for any template's numbering.
        research_context += "\n" + format_images_for_context(
            image_hits,
            require_at_least_one=_section_wants_diagram(blocks, sec_lookup=sec_lookup),
        )

    if is_combined:
        task_instruction = (
            "Draft the full content for every sub-part listed above now, in one response, "
            "each under its own marker line as specified. Do not repeat the top-level section "
            "title anywhere. If a sub-part uses a table, use Markdown table syntax so it "
            "converts cleanly to a real Word table on export. Keep each sub-part as short as "
            "the density rules above allow — shorter and precise beats longer and padded "
            "every time."
        )
    else:
        task_instruction = (
            "Draft the full content for this ONE SOW section now — just this section, not the "
            "whole document. Do not repeat the section title as a heading in your answer (the "
            "caller applies the heading itself). If this section uses a table, use Markdown "
            "table syntax so it converts cleanly to a real Word table on export. Keep it as "
            "short as the density rules above allow — shorter and precise beats longer and "
            "padded every time."
        )
    prompt = f"{research_context}\n\n{task_instruction}\n"

    if is_combined:
        max_tokens = min(
            sum(SOW_MAX_OUTPUT_TOKENS_TABLE if b in table_ids else SOW_MAX_OUTPUT_TOKENS
                for b in blocks),
            SOW_MAX_OUTPUT_TOKENS_COMBINED_CAP,
        )
    else:
        max_tokens = SOW_MAX_OUTPUT_TOKENS_TABLE if is_table else SOW_MAX_OUTPUT_TOKENS
    output = completion_from_prompt(
        prompt,
        system_prompt=SOW_SKILL_TEXT,
        max_tokens=max_tokens,
        temperature=0.2,
        max_output_cap=max_tokens,
    )
    output = _sanitize_locators(output)

    # Persist as a pending draft — not approved — so it survives a page
    # refresh or a background bulk-draft run and is there for the reviewer
    # to open, edit, and approve (or discard) whenever they get to it. Kept
    # under the top-level section_id even for a combined family — splitting
    # onto each sub-part's own id happens once, authoritatively, at approve
    # time against whatever the reviewer actually submits.
    kw = _load_keyword_store(project_id)
    kw.setdefault(section_id, {})
    kw[section_id]["status"] = "in-progress"
    kw[section_id]["custom_instructions"] = req.custom_instructions
    kw[section_id]["draft_content"] = output
    kw[section_id]["draft_sources"] = [c.get("doc_name", "") for c in matches]
    _save_keyword_store(kw, project_id)

    return {
        "section_id": section_id,
        "content": output,
        "sources": [c.get("doc_name", "") for c in matches],
        "image_ids": [h.get("image_id", "") for h in image_hits],
    }


@router.post("/sections/{section_id}/approve")
def approve_sow_section(section_id: str, req: ApproveRequest):
    project_id = req.project_id
    sections_store = _load_sections_store(project_id)
    active_sections = sections_store.get("active", [])
    sec = next((s for s in active_sections if s["id"] == section_id), None)
    if not sec:
        raise HTTPException(404, f"Section '{section_id}' not found for this project")
    if not _is_top_level(section_id):
        raise HTTPException(
            400,
            "Approve the parent section instead — its sub-sections are approved together.",
        )
    if not req.content or not req.content.strip():
        raise HTTPException(400, "Cannot approve empty content")

    family = _family_ids(section_id, active_sections)
    blocks = _draftable_blocks(section_id, active_sections)
    sec_lookup = {s["id"]: s for s in active_sections}

    approved = _load_approved_store(project_id)
    approved.setdefault("sections", {})
    now = datetime.utcnow().isoformat()

    if len(blocks) == 1:
        approved["sections"][blocks[0]] = {"content": req.content.strip(), "approved_at": now}
    else:
        split = _split_combined_content(req.content, blocks)
        missing = [bid for bid in blocks if not split.get(bid, "").strip()]
        if missing:
            missing_titles = [f"{bid} {sec_lookup[bid]['title']}" for bid in missing]
            raise HTTPException(
                400,
                "Could not find the sub-heading marker for: " + ", ".join(missing_titles) +
                '. Keep each "### {id} {title}" line intact when editing — one per sub-part.',
            )
        for bid in blocks:
            approved["sections"][bid] = {"content": split[bid], "approved_at": now}
    _save_approved_store(approved, project_id)

    kw = _load_keyword_store(project_id)
    kw.setdefault(section_id, {})
    kw[section_id]["status"] = "generated"
    kw[section_id].pop("draft_content", None)
    kw[section_id].pop("draft_sources", None)
    # Mirror status onto each family member's own kw entry too, for any other
    # code path that might read child-level status directly.
    for bid in family:
        kw.setdefault(bid, {})
        kw[bid]["status"] = "generated" if bid in approved["sections"] else kw[bid].get("status", "not-started")
    _save_keyword_store(kw, project_id)

    from app.project_routes import PROJECTS, _save_state
    if project_id in PROJECTS:
        PROJECTS[project_id]["updated"] = datetime.utcnow().isoformat()
        _save_state()

    logger.info("Approved SOW section %s (family: %s) for project %s", section_id, blocks, project_id)
    return {"ok": True, "section_id": section_id}


# ─────────────────────────────────────────────────────────────────────────────
# Bulk "draft all remaining" background runner — GENERATES a draft for each
# pending section but deliberately does NOT approve any of them. Approval is
# always a separate, individual, human action (see approve_sow_section) — a
# section-by-section tool that let a bulk button silently approve everything
# would defeat the entire point of the review step. This just saves the
# reviewer from clicking "Generate" on every section one at a time; they
# still open, edit if needed, and approve each one themselves.
# ─────────────────────────────────────────────────────────────────────────────
_GEN_LOCK = threading.Lock()
_GEN_RUNS: Dict[str, Dict] = {}


def _generation_worker(project_id: str, section_ids: List[str]) -> None:
    run = _GEN_RUNS[project_id]
    for sid in section_ids:
        if not run["running"]:
            break
        run["current"] = sid
        run["log"].append(f"Drafting {sid}…")
        try:
            generate_sow_section(sid, GenerateRequest(project_id=project_id))
            run["log"].append(f"Draft ready for {sid} — awaiting your review")
        except Exception as exc:
            run["log"].append(f"Failed {sid}: {exc}")
            logger.exception("SOW bulk drafting failed for section %s (project %s)", sid, project_id)
    run["running"] = False
    run["current"] = None


@router.post("/generation/start")
def start_sow_generation(req: BulkGenerateRequest):
    project_id = req.project_id
    with _GEN_LOCK:
        if _GEN_RUNS.get(project_id, {}).get("running"):
            raise HTTPException(409, "Generation already running for this project")
        _ensure_seeded(project_id)
        sections_store = _load_sections_store(project_id)
        approved = _load_approved_store(project_id)
        active_sections = sections_store.get("active", [])
        approved_sections = approved.get("sections", {})
        top_level_ids = [s["id"] for s in active_sections if _is_top_level(s["id"])]

        def _is_pending(sid: str) -> bool:
            blocks = _draftable_blocks(sid, active_sections)
            return not blocks or not all(bid in approved_sections for bid in blocks)

        pending = req.section_ids or [sid for sid in top_level_ids if _is_pending(sid)]
        pending = [sid for sid in pending if _is_top_level(sid) and _is_pending(sid)]
        if not pending:
            return {"ok": True, "started": 0, "message": "Nothing pending — all sections already approved"}
        _GEN_RUNS[project_id] = {"running": True, "log": [], "current": None}
        t = threading.Thread(target=_generation_worker, args=(project_id, pending), daemon=True)
        t.start()
    return {"ok": True, "started": len(pending)}


@router.get("/generation/status")
def sow_generation_status(project_id: str):
    return _GEN_RUNS.get(project_id, {"running": False, "log": [], "current": None})


@router.post("/generation/stop")
def stop_sow_generation(project_id: str):
    if project_id in _GEN_RUNS:
        _GEN_RUNS[project_id]["running"] = False
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Used by app/sow_workflow.py (export) — ordered list of approved sections.
# ─────────────────────────────────────────────────────────────────────────────

def get_ordered_approved_sections(project_id: str) -> List[Dict]:
    sections_store = _load_sections_store(project_id)
    approved = _load_approved_store(project_id)
    out = []
    for s in sections_store.get("active", []):
        content = approved.get("sections", {}).get(s["id"], {}).get("content")
        if content:
            out.append({"id": s["id"], "title": s["title"], "level": s.get("level", 1), "content": content})
    return out


def get_all_active_sections_with_status(project_id: str) -> List[Dict]:
    """Like get_ordered_approved_sections, but includes EVERY active section
    (not just approved ones) — content is None for anything not yet
    approved. Lets a partial export mark those headings as "In Progress"
    (sow_workflow.py) instead of silently skipping them and leaving the
    template's own stale placeholder content in their place.

    Carries `has_own_content` through too: a container heading with no body
    slot of its own (e.g. "3. Project Overview", whose real content lives in
    child sub-sections 3.1/3.2/3.3 — see _draftable_blocks) never gets
    approved content and never will, by design. Without this flag the
    export would wrongly flag every such container as "In Progress" even in
    a fully-approved document — sow_workflow.py only inserts the notice when
    this is True (or absent, for older non-real-template projects where the
    field was never populated)."""
    sections_store = _load_sections_store(project_id)
    approved = _load_approved_store(project_id)
    out = []
    for s in sections_store.get("active", []):
        content = approved.get("sections", {}).get(s["id"], {}).get("content")
        out.append({
            "id": s["id"], "title": s["title"], "level": s.get("level", 1),
            "content": content, "has_own_content": s.get("has_own_content", True),
        })
    return out


def get_sections_for_review(project_id: str) -> List[Dict]:
    """Like get_all_active_sections_with_status, but falls back to a section's
    UNAPPROVED draft text when it has no approved content yet.

    The review checks (legal baseline, US-04/05/06 scope checks) are meant to
    be run while drafting, not only after every section is signed off — an
    author who has to approve a section before they can find out it is missing
    its deemed-acceptance clause will approve first and discover later. A
    combined family's draft lives on the parent id as one marker-delimited
    blob, so it is split back onto each block's own id with the same splitter
    the approve step uses; otherwise a child like "4.8 Dependencies" would
    look empty right up until approval.
    """
    sections = get_all_active_sections_with_status(project_id)
    kw = _load_keyword_store(project_id)
    active = _load_sections_store(project_id).get("active", [])
    by_id = {s["id"]: s for s in sections}

    for parent_id, entry in kw.items():
        draft = (entry or {}).get("draft_content")
        if not draft:
            continue
        blocks = _draftable_blocks(parent_id, active)
        pieces = _split_combined_content(draft, blocks) if len(blocks) > 1 else {parent_id: draft}
        if len(blocks) > 1 and not pieces:
            # Markers absent (an older or hand-edited draft) — keep the whole
            # blob on the parent rather than dropping it entirely.
            pieces = {parent_id: draft}
        for bid, text in pieces.items():
            sec = by_id.get(bid)
            if sec is not None and not (sec.get("content") or "").strip():
                sec["content"] = text
                sec["content_is_draft"] = True
    return sections


# ─────────────────────────────────────────────────────────────────────────────
# Export — assemble approved sections into a real .docx, using whichever
# template (the project's own custom upload, else the org default, else the
# from-scratch fallback builder) resolved for this project. Unapproved
# sections are still passed through (as content=None) so the template's own
# headings for them get an "In Progress" notice instead of being skipped.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/document/export")
def export_sow_document(project_id: str):
    _ensure_seeded(project_id)
    if not get_ordered_approved_sections(project_id):
        raise HTTPException(400, "No approved sections yet — approve at least one section before exporting")
    sections = get_all_active_sections_with_status(project_id)

    from app.project_routes import PROJECTS
    client_name = PROJECTS.get(project_id, {}).get("client", "") or ""
    project_name = PROJECTS.get(project_id, {}).get("name", "") or ""

    from app.sow_template_routes import get_template_file_path
    from app.sow_workflow import build_sow_docx

    template_id = get_project_template_id(project_id)
    template_path = get_template_file_path(template_id) if template_id else None

    try:
        out_path = build_sow_docx(project_id, sections, client_name, template_path, project_name)
    except Exception as exc:
        logger.exception("SOW export failed for project %s", project_id)
        raise HTTPException(500, f"Export failed: {exc}")

    logger.info("Exported SOW for project %s → %s (template=%s)",
                project_id, out_path.name, template_id or "fallback")
    return {"ok": True, "docx_url": f"/api/document/download/docx/{out_path.name}"}


# ─────────────────────────────────────────────────────────────────────────────
# Per-section reference attachments — a document, image, or flowchart the
# reviewer attaches to one specific section (which may be a leaf top-level id
# like "5" or a sub-section id like "3.3"), injected as PRIORITY context into
# that block's own generation prompt (see generate_sow_section's
# _format_attachment_context usage). Images reuse the same physical store and
# <IMAGE id="..."/> embed mechanism the rest of the app already relies on, so
# no changes were needed to image resolution/export.
# ─────────────────────────────────────────────────────────────────────────────

def _attachment_kind(filename: str) -> Optional[str]:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext in _ATTACHMENT_IMAGE_EXTS:
        return "image"
    if ext in _ATTACHMENT_DOC_EXTS:
        return "document"
    return None


@router.post("/sections/{section_id}/attachments")
async def upload_sow_attachment(
    section_id: str,
    project_id: str = Form(...),
    file: UploadFile = File(...),
    caption: str = Form(""),
):
    sections_store = _load_sections_store(project_id)
    active_sections = sections_store.get("active", [])
    sec = next((s for s in active_sections if s["id"] == section_id), None)
    if not sec:
        raise HTTPException(404, f"Section '{section_id}' not found for this project")

    filename = file.filename or "attachment"
    kind = _attachment_kind(filename)
    if not kind:
        raise HTTPException(
            400,
            "Unsupported file type — attach an image (png/jpg/gif/bmp/webp) or a "
            "reference document (docx/pdf/pptx/xlsx).",
        )
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(400, "Uploaded file is empty")

    ext = filename.rsplit(".", 1)[-1].lower()
    attachment_id = uuid.uuid4().hex[:12]
    upload_dir = _SOW_ATTACHMENT_UPLOAD_ROOT / project_id / "attachments"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / f"{attachment_id}.{ext}"
    saved_path.write_bytes(raw_bytes)

    record: Dict = {
        "attachment_id": attachment_id,
        "kind": kind,
        "filename": filename,
        "file_path": str(saved_path),
        "caption": caption.strip(),
        "uploaded_at": datetime.utcnow().isoformat(),
    }

    if kind == "image":
        from app.image_extractor import ImageExtractor
        extractor = ImageExtractor(output_dir=str(_EXTRACTED_IMAGES_DIR))
        img_meta = extractor._save_image_record(raw_bytes, ext, source_doc=filename)
        if not img_meta:
            raise HTTPException(400, "Image could not be processed (unsupported format or size)")
        vision_data = extractor._minimal_vision_record(
            img_meta["file_path"], source_context=caption.strip() or filename
        )
        index_record = {**img_meta, **vision_data}
        index_record.pop("image_path", None)
        index_record["sow_section_id"] = section_id
        index_record["sow_project_id"] = project_id
        extractor.append_to_index([index_record], index_file=str(_IMAGE_CHUNKS_PATH))
        try:
            from app.image_index import reload_image_index
            reload_image_index()
        except Exception as exc:
            logger.warning("Could not reload image index after attachment upload: %s", exc)
        record["image_id"] = img_meta["image_id"]
    else:
        try:
            from app.chunking_pipeline import extract_text_from_file
            blocks = extract_text_from_file(saved_path)
            extracted_text = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
        except Exception as exc:
            logger.warning("Could not extract text from attachment %s: %s", filename, exc)
            extracted_text = ""
        # Capped and kept ONLY on this attachment record — deliberately not
        # merged into the project's general chunks.json, so it stays scoped
        # as priority context for this one section rather than leaking into
        # every other section's general retrieval.
        record["extracted_text"] = extracted_text[:4000]

        # Also pull any embedded images/flowcharts (e.g. PPTX slide diagrams,
        # DOCX figures) out of the attached document and index them the same
        # way a directly-uploaded image is, so the LLM can place them via
        # <IMAGE id="..."/> just like the kind=="image" branch above. Without
        # this, an attached PPTX only ever contributed its text — every
        # embedded diagram was silently dropped.
        try:
            from app.image_extractor import ImageExtractor
            extractor = ImageExtractor(output_dir=str(_EXTRACTED_IMAGES_DIR))
            image_records = extractor.process_single_file(str(saved_path))
            for img_rec in image_records:
                # process_single_file names source_doc after the on-disk path
                # (the attachment_id-based filename); restore the original,
                # human-readable filename so Browse Images and captions make
                # sense, and tag it to this section like the image branch does.
                img_rec["source_doc"] = filename
                img_rec["sow_section_id"] = section_id
                img_rec["sow_project_id"] = project_id
            extractor.append_to_index(image_records, index_file=str(_IMAGE_CHUNKS_PATH))
            image_ids = [r["image_id"] for r in image_records if r.get("image_id")]

            # An attached deck gets whole-slide rendering too, for the same
            # reason project uploads do: a native-shape flowchart has no
            # embedded image to extract. Best-effort — see slide_renderer.
            if ext in ("pptx", "ppt"):
                try:
                    from app.slide_renderer import (
                        pick_diagram_slides, render_slides,
                    )
                    import tempfile
                    picks = pick_diagram_slides(str(saved_path))
                    if picks:
                        with tempfile.TemporaryDirectory(prefix="sow_slide_") as tmp:
                            for slide_no, png in sorted(
                                    render_slides(str(saved_path), tmp, picks).items()):
                                meta = extractor._save_image_record(
                                    Path(png).read_bytes(), "png",
                                    source_doc=filename, page_number=slide_no,
                                )
                                if not meta:
                                    continue
                                vision = extractor._minimal_vision_record(
                                    meta["file_path"],
                                    source_context=f"{filename}, slide {slide_no}",
                                )
                                rec = {**meta, **vision}
                                rec.pop("image_path", None)
                                rec["render_kind"] = "slide"
                                rec["sow_section_id"] = section_id
                                rec["sow_project_id"] = project_id
                                extractor.append_to_index(
                                    [rec], index_file=str(_IMAGE_CHUNKS_PATH))
                                image_ids.append(meta["image_id"])
                except Exception as exc:
                    logger.warning("Whole-slide rendering skipped for attachment "
                                   "%s: %s", filename, exc)

            if image_ids:
                record["image_ids"] = image_ids
                from app.image_index import reload_image_index
                reload_image_index()
        except Exception as exc:
            logger.warning("Could not extract images from attachment %s: %s", filename, exc)

    store = _load_attachments_store(project_id)
    store.setdefault("attachments", {}).setdefault(section_id, []).append(record)
    _save_attachments_store(store, project_id)

    logger.info("SOW attachment uploaded: project=%s section=%s kind=%s file=%s",
                project_id, section_id, kind, filename)

    response_record = {k: v for k, v in record.items() if k not in ("extracted_text", "file_path")}
    return {"ok": True, "attachment": response_record}


@router.get("/sections/attachments")
def list_sow_attachments(project_id: str):
    store = _load_attachments_store(project_id)
    out = {}
    for sid, atts in store.get("attachments", {}).items():
        out[sid] = [
            {k: v for k, v in att.items() if k not in ("extracted_text", "file_path")}
            for att in atts
        ]
    return {"attachments": out}


@router.delete("/sections/{section_id}/attachments/{attachment_id}")
def delete_sow_attachment(section_id: str, attachment_id: str, project_id: str):
    store = _load_attachments_store(project_id)
    atts = store.get("attachments", {}).get(section_id, [])
    match = next((a for a in atts if a.get("attachment_id") == attachment_id), None)
    if not match:
        raise HTTPException(404, "Attachment not found")

    store["attachments"][section_id] = [a for a in atts if a.get("attachment_id") != attachment_id]
    _save_attachments_store(store, project_id)

    try:
        fp = Path(match.get("file_path", ""))
        if fp.exists():
            fp.unlink()
    except Exception as exc:
        logger.warning("Could not delete attachment file %s: %s", match.get("file_path"), exc)

    # Key on (image_id, source_doc) — the same pair append_to_index dedupes
    # on — not image_id alone. image_id is a hash of the raw bytes, so an
    # identical image (e.g. a shared logo, or the same deck already indexed
    # from a project-level upload) can legitimately have several index
    # records under the same image_id but different source_doc. Matching by
    # image_id alone would delete every other document's record too.
    source_doc_key = (match.get("filename") or "").lower()
    id_pairs_to_remove: set = set()
    if match.get("kind") == "image" and match.get("image_id"):
        id_pairs_to_remove.add((match["image_id"], source_doc_key))
    if match.get("kind") == "document" and match.get("image_ids"):
        id_pairs_to_remove.update((iid, source_doc_key) for iid in match["image_ids"])

    if id_pairs_to_remove:
        # Attachment-scoped images aren't shared with any library — full
        # removal from the shared index is correct here, not just untagging.
        # _save_image_record wrote a SEPARATE physical copy into
        # extracted_images/ (the one <IMAGE id="..."/> embedding actually
        # resolves against) distinct from the sow_uploads/ copy removed
        # above — both must be cleaned up or the extracted_images/ copy
        # leaks forever once its index record is gone.
        try:
            def _pair(c):
                return (c.get("image_id"), (c.get("source_doc") or "").lower())

            chunks = _load_json(_IMAGE_CHUNKS_PATH, [])
            removed_records = [c for c in chunks if _pair(c) in id_pairs_to_remove]
            remaining = [c for c in chunks if _pair(c) not in id_pairs_to_remove]
            _save_json(_IMAGE_CHUNKS_PATH, remaining)
            from app.image_index import reload_image_index
            reload_image_index()
            # Only unlink the physical file if no surviving record (from a
            # different source_doc that happens to share the same binary,
            # hence the same image_id) still points at it — one physical
            # file is shared across every record with that image_id.
            remaining_ids = {c.get("image_id") for c in remaining}
            for image_record in removed_records:
                if image_record.get("image_id") in remaining_ids:
                    continue
                fp = image_record.get("file_path")
                if fp:
                    extracted_fp = Path(fp)
                    if extracted_fp.exists():
                        extracted_fp.unlink()
        except Exception as exc:
            logger.warning("Could not remove attachment image(s) %s from index: %s",
                           id_pairs_to_remove, exc)

    return {"ok": True}


@router.get("/sections/attachments/{attachment_id}/file")
def serve_sow_attachment_file(attachment_id: str, project_id: str):
    store = _load_attachments_store(project_id)
    for atts in store.get("attachments", {}).values():
        for att in atts:
            if att.get("attachment_id") == attachment_id:
                fp = Path(att.get("file_path", ""))
                if not fp.exists():
                    raise HTTPException(404, "Attachment file missing on disk")
                return FileResponse(path=str(fp), filename=att.get("filename"))
    raise HTTPException(404, "Attachment not found")


@router.get("/images/{image_id}")
def serve_sow_inline_image(image_id: str):
    """Resolver for <IMAGE id="..."/> references inside a section preview —
    mirrors image_repo_routes.serve_repo_image but without repo-scoping,
    since a section-attached image (see upload_sow_attachment) is never
    tagged to any image_repo_id."""
    for ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
        cand = _EXTRACTED_IMAGES_DIR / f"{image_id}.{ext}"
        if cand.exists():
            return FileResponse(path=str(cand), media_type=f"image/{ext}")
    raise HTTPException(404, "Image not found")


# ─────────────────────────────────────────────────────────────────────────────
# Per-section HTML preview — an approximation of how a section will look in
# the exported .docx (template fonts/colors, tables, inline images), rendered
# BEFORE export so a reviewer can sanity-check a section (and any attached
# image) right after generating/approving it. Deliberately an HTML
# approximation, not a real docx→image render.
# ─────────────────────────────────────────────────────────────────────────────

_PREVIEW_IMAGE_TAG_RE = re.compile(r'<IMAGE\s+id="([^"]+)"\s*/?>', re.IGNORECASE)


def _bold_html(text: str) -> str:
    """Render **bold** spans as <strong>, html.escape()-ing every text piece
    (per-segment now instead of the whole string) — shares
    sow_markdown.split_bold_segments with the .docx export path, so Preview
    and the final Word doc agree on what's bold instead of both showing
    literal asterisks as they did before."""
    from app.sow_markdown import split_bold_segments
    out = []
    for seg_text, is_bold in (split_bold_segments(text) or [("", False)]):
        escaped = html.escape(seg_text)
        out.append(f"<strong>{escaped}</strong>" if is_bold else escaped)
    return "".join(out)


def _render_text_with_images(text: str) -> str:
    """Render free text (bold-aware, always escaped — see _bold_html), but
    resolve any <IMAGE id="..."/> tag within it to an actual <img> pointing
    at the section-image resolver above."""
    out = []
    last = 0
    for m in _PREVIEW_IMAGE_TAG_RE.finditer(text):
        out.append(_bold_html(text[last:m.start()]))
        out.append(f'<img class="pv-inline-img" src="/api/sow/images/{html.escape(m.group(1))}" />')
        last = m.end()
    out.append(_bold_html(text[last:]))
    return "".join(out)


def _render_markdown_table_html(table_lines: List[str], style_profile: Dict) -> str:
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in table_lines]
    if len(rows) < 2:
        return ""
    header, sep, *data_rows = rows
    # A markdown separator row is all dashes/colons/spaces per cell — guard
    # against a genuine data row being mistaken for the separator.
    if not all(set(c) <= set("-: ") for c in sep):
        data_rows = [sep] + data_rows
    th_style = (f'background:{style_profile["table_header_bg"]};'
                f'color:{style_profile["table_header_color"]};font-weight:700;'
                f'padding:6px 10px;border:1px solid #ccc;text-align:center;')
    td_style = 'padding:6px 10px;border:1px solid #ccc;text-align:left;'
    thead = "<tr>" + "".join(f'<th style="{th_style}">{_bold_html(c)}</th>' for c in header) + "</tr>"
    tbody = "".join(
        "<tr>" + "".join(f'<td style="{td_style}">{_bold_html(c)}</td>' for c in r) + "</tr>"
        for r in data_rows if any(c for c in r)
    )
    return f'<table style="border-collapse:collapse;width:100%;margin:10px 0;font-size:{style_profile["body_size_pt"]}pt">{thead}{tbody}</table>'


def _render_block_body_html(content: str, style_profile: Dict) -> str:
    lines = [ln.rstrip() for ln in (content or "").split("\n")]
    parts = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|") and line.count("|") >= 2:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().count("|") >= 2:
                table_lines.append(lines[i].strip())
                i += 1
            parts.append(_render_markdown_table_html(table_lines, style_profile))
            continue
        if line.startswith(("- ", "* ", "• ")):
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ", "• ")):
                items.append(_render_text_with_images(lines[i].strip()[2:].strip()))
                i += 1
            parts.append("<ul style='margin:6px 0 6px 20px'>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            continue
        parts.append(f"<p style='margin:0 0 8px'>{_render_text_with_images(line)}</p>")
        i += 1
    return "\n".join(parts)


def render_section_preview_html(combined_text: str, family: List[Dict], style_profile: Dict) -> str:
    """`family`: [{"id", "title"}, ...] in document order for the blocks
    actually being previewed. Splits combined_text via _split_combined_content
    when there's more than one block (same marker convention approval uses);
    a single-block family is rendered as-is with no split attempted."""
    block_ids = [f["id"] for f in family]
    if len(family) > 1:
        split = _split_combined_content(combined_text, block_ids)
    else:
        split = {family[0]["id"]: combined_text} if family else {}

    heading_style = (
        f'font-family:{style_profile["heading_font"]},sans-serif;'
        f'font-size:{style_profile["heading_size_pt"]}pt;'
        f'color:{style_profile["heading_color"]};font-weight:700;margin:18px 0 8px'
    )
    body_style = (
        f'font-family:{style_profile["body_font"]},sans-serif;'
        f'font-size:{style_profile["body_size_pt"]}pt;'
        f'color:{style_profile["body_color"]};line-height:1.5'
    )

    parts = [f'<div style="{body_style}">']
    for f in family:
        bid = f["id"]
        content = (split.get(bid) or "").strip()
        if not content:
            continue
        parts.append(f'<h3 style="{heading_style}">{html.escape(bid)} {html.escape(f["title"])}</h3>')
        parts.append(_render_block_body_html(content, style_profile))
    parts.append("</div>")
    return "\n".join(parts)


def _resolve_combined_text(project_id: str, section_id: str):
    """Shared by the preview endpoint and the raw-content endpoint below:
    the pending draft if one exists, else the family's already-approved
    content re-assembled with the same "### {id} {title}" marker convention
    used everywhere else (generate/approve/preview) — so whichever caller
    gets this string back, it round-trips through approve_sow_section
    exactly like a normal draft would. Returns (combined_text, family)."""
    _ensure_seeded(project_id)
    sections_store = _load_sections_store(project_id)
    active_sections = sections_store.get("active", [])
    sec = next((s for s in active_sections if s["id"] == section_id), None)
    if not sec:
        raise HTTPException(404, f"Section '{section_id}' not found for this project")
    if not _is_top_level(section_id):
        raise HTTPException(400, "Use the parent section instead.")

    blocks = _draftable_blocks(section_id, active_sections)
    sec_lookup = {s["id"]: s for s in active_sections}
    family = [{"id": bid, "title": sec_lookup[bid]["title"]} for bid in blocks]

    kw = _load_keyword_store(project_id)
    approved = _load_approved_store(project_id)
    approved_sections = approved.get("sections", {})

    draft = kw.get(section_id, {}).get("draft_content")
    if draft:
        combined_text = draft
    elif len(blocks) > 1:
        parts = []
        for bid in blocks:
            content = approved_sections.get(bid, {}).get("content", "")
            if content:
                parts.append(_marker_line(bid, sec_lookup[bid]["title"]))
                parts.append(content)
        combined_text = "\n\n".join(parts)
    else:
        combined_text = approved_sections.get(blocks[0], {}).get("content", "") if blocks else ""

    return combined_text, family


@router.get("/sections/{section_id}/preview")
def preview_sow_section(section_id: str, project_id: str):
    combined_text, family = _resolve_combined_text(project_id, section_id)
    if not combined_text.strip():
        raise HTTPException(400, "Nothing to preview yet — generate or approve this section first.")

    from app.sow_style import get_template_style_profile
    from app.sow_template_routes import get_template_file_path

    template_id = get_project_template_id(project_id)
    template_path = get_template_file_path(template_id) if template_id else None
    style_profile = get_template_style_profile(template_id, template_path)

    html_out = render_section_preview_html(combined_text, family, style_profile)
    return {"html": html_out, "family_ids": [f["id"] for f in family]}


@router.get("/sections/{section_id}/content")
def get_sow_section_content(section_id: str, project_id: str):
    """Raw (non-HTML) combined text for a section — the pending draft if
    one exists, else its already-approved content. Used by the full-screen
    editor: the review pane's #draft-text is deliberately left empty for an
    already-approved section with no pending draft (see list_sow_sections/
    renderContent — "click Generate to redraft"), so without this endpoint
    the editor opened on exactly that, most common case (a section someone
    wants to revisit and tweak) with nothing in it."""
    combined_text, _family = _resolve_combined_text(project_id, section_id)
    if not combined_text.strip():
        raise HTTPException(400, "Nothing to edit yet — generate or approve this section first.")
    return {"content": combined_text}
