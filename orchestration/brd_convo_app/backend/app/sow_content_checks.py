"""
sow_content_checks.py — SOW content checks (US-04 … US-10)

Scope boundaries, acceptance, change control, commercial completeness,
governance, risk ownership and performance metrics — everything an acceptance
criterion asserts about the finished document rather than about the tool.

Drafting guidance tells the model what to write; these checks verify what it
actually wrote. Guidance alone is not enforcement — a model that is asked for
a review window in business days will usually give one, and the times it does
not are exactly the times nobody notices. So each acceptance criterion that
can be read off the text gets a check here, and failures surface as flags on
the review gate (US-03) next to the legal-baseline deviations.

WHAT IS CHECKED

US-04 — scope definition
  * all five areas present with real content: Scope, Deliverables,
    Assumptions, Dependencies, Exclusions
  * Exclusions state what is NOT in scope, in negative wording
  * every Dependencies row names an owning party — Customer, Bristlecone or a
    named third party
  * generic boilerplate that would read the same on any client's SOW

US-05 — acceptance and deemed acceptance
  * acceptance criteria are measurable, not "to the client's satisfaction"
  * the review window is an explicit number of days, business or calendar
  * a deemed-acceptance clause fires automatically when the window expires

US-06 — change requests
  * the CR workflow, its approvers and a turnaround time are all defined
  * commercial AND timeline impact assessment is mandatory
  * no work starts on an unapproved change

US-07 — commercial terms
  * the billing model is stated
  * payment terms give a credit period in days
  * a delayed-payment clause names an interest rate or other consequence
  * currency, tax treatment and expense treatment are all stated

US-08 — governance and escalation
  * governance forums list a cadence and participants
  * escalation levels 1-3 each name roles and a response timeline
  * contact details exist for both parties

US-09 — risk register
  * risks carry both impact and likelihood
  * each risk states a mitigation approach
  * each risk names an owner by party and role

US-10 — KPIs, SLAs and success metrics
  * metrics are measurable, with targets and a measurement frequency
  * measurement method and data source are defined
  * SLA breach consequences are stated, or their absence is explicit

US-11 — onboarding and access provisioning
  * onboarding steps carry an expected duration
  * customer obligations cover access, licences and VPN with timelines
  * the impact of access delays on schedule AND billing is stated

US-12 — asset management and return
  * issuance and tracking are described
  * return is covered for both project closure and resource rollover, with a
    timeline
  * accountability for loss or non-return is stated

US-13 — term and termination
  * start date, duration and the renewal mechanism are stated
  * termination for cause and for convenience are both covered, with notice
  * exit obligations cover knowledge transfer, data return or deletion, asset
    return and final settlement

US-14 — back-to-back vendor alignment
  * vendor scope, SLAs and acceptance mirror the customer SOW
  * deliverable-based acceptance is not replaced by timesheet approval
  * vendor payment terms are stated so credit-period compatibility is visible

US-15 — contractor and vendor compliance
  * POSH, Code of Conduct, ISMS and PIMS are all named and binding
  * evidence of acknowledgement is required before access is granted
  * non-compliance consequences and audit rights are stated

MATCHING SECTIONS
Sections are found by id first (the built-in templates' own numbering) and
then by title keywords, so a client's .docx template that numbers things
differently is still checked. A section that genuinely cannot be located is
reported as `missing` rather than silently passing — the failure mode this
module exists to prevent is a check that quietly finds nothing and reports
success.

Every flag is advisory: it points a reviewer at a specific sentence. Nothing
here edits the draft or blocks generation.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SOWScopeChecks")

_WS = re.compile(r'\s+')
_NUM_PREFIX = re.compile(
    r'^\s*(?:section\s+)?(?:\d+(?:\.\d+)*|appendix\s+[a-z])[\.\)\:\-—–\s]+', re.I)


def _norm(t: str) -> str:
    t = _NUM_PREFIX.sub("", t or "")
    t = t.replace("&", " and ")
    t = re.sub(r'[^\w\s]', " ", t)
    return _WS.sub(" ", t).strip().lower()


# area -> (preferred ids, title keywords that identify it, titles that must NOT match)
_AREAS: Dict[str, Tuple[List[str], List[str], List[str]]] = {
    "scope":        (["4"],   ["scope of work", "project scope", "services scope"],
                     ["out of scope", "exclusion"]),
    "deliverables": (["9"],   ["deliverable"], []),
    "assumptions":  (["10"],  ["assumption"], []),
    "dependencies": (["4.8"], ["dependenc"], []),
    "exclusions":   (["4.7"], ["exclusion", "out of scope", "not in scope"], []),
    "acceptance":   (["11"],  ["acceptance criteria", "acceptance"],
                     ["acceptance and sign", "sign-off"]),
    "change":       (["17"],  ["change management", "change request", "change control"], []),
    "commercials":  (["13"],  ["commercial", "fees and payment", "pricing", "charges"], []),
    "governance":   (["7"],   ["project management", "governance"], ["risk mitigation"]),
    "escalation":   (["7.4"], ["escalation"], []),
    "contacts":     (["7.5"], ["key contact", "contact"], []),
    "risks":        (["15"],  ["risk"], ["risk mitigation", "mitigation"]),
    "kpis":         (["14"],  ["performance reporting", "kpi", "sla", "service level"], []),
    "onboarding":   (["8.1"], ["onboarding", "access provisioning", "mobilisation",
                               "mobilization"], []),
    "assets":       (["8.2"], ["asset"], []),
    "agreement":    (["1"],   ["agreement details"], []),
    "termination":  (["19"],  ["termination", "term and termination", "term & termination"],
                     []),
    "vendor_alignment": (["12.3"], ["vendor alignment", "subcontractor", "back-to-back",
                                    "vendor contract"], []),
    "vendor_compliance": (["12.4"], ["vendor compliance", "contractor compliance",
                                     "contractor and vendor"], []),
}

_AREA_LABEL = {
    "scope": "Scope of Work", "deliverables": "Key Deliverables",
    "assumptions": "Key Assumptions", "dependencies": "Dependencies",
    "exclusions": "Exclusions", "acceptance": "Acceptance Criteria",
    "change": "Change Management Process", "commercials": "Commercials",
    "governance": "Governance & Forums", "escalation": "Escalation Matrix",
    "contacts": "Key Contacts", "risks": "Risk Register",
    "kpis": "KPIs / SLAs", "onboarding": "Onboarding & Access",
    "assets": "Asset Management & Return", "agreement": "Agreement Details",
    "termination": "Term & Termination",
    "vendor_alignment": "Vendor Alignment", "vendor_compliance": "Vendor Compliance",
}

# An owning party on a dependency row must resolve to one of these.
_OWNER_PATTERNS = [
    re.compile(r'\bcustomer\b', re.I), re.compile(r'\bclient\b', re.I),
    re.compile(r'\bbristlecone\b', re.I), re.compile(r'\bsupplier\b', re.I),
    re.compile(r'\bvendor\b', re.I), re.compile(r'\bthird[\s\-]?party\b', re.I),
    re.compile(r'\bjoint\b', re.I), re.compile(r'\bboth parties\b', re.I),
]
_OWNER_COL_HINTS = ["owning party", "owner", "responsible party", "responsible",
                    "accountable", "party"]

# Phrases that mean nothing on their own — the boilerplate this story targets.
_BOILERPLATE = [
    "industry best practice", "as per standard practice", "as applicable",
    "as required", "where necessary", "and other related activities",
    "etc.", "high quality", "in a timely manner", "as mutually agreed",
    "standard methodology", "usual and customary",
]
_VAGUE_ACCEPTANCE = [
    "client satisfaction", "customer satisfaction", "to the satisfaction of",
    "works as expected", "high quality", "acceptable quality",
    "meets expectations", "satisfactory to",
]

_DAYS_RE = re.compile(
    r'\b(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty)\b'
    r'[^.\n]{0,40}?\b(business|working|calendar)\s+days?\b', re.I)
_BARE_DAYS_RE = re.compile(r'\b(?:\d{1,3})\s*(?:\(\s*\w+\s*\)\s*)?days?\b', re.I)
_DEEMED_RE = re.compile(r'deem(?:ed|s|)\s+(?:to\s+be\s+)?accept', re.I)
_AUTO_RE = re.compile(r'\bautomatic(?:ally)?\b|\bon expiry\b|\bupon expiry\b|'
                      r'\bwithout further\b|\blapse\b', re.I)
_PLACEHOLDER_RE = re.compile(r'<\s*CONFIRM\s*:', re.I)

# "No work begins on an unapproved change" gets written many ways. Real drafts
# produce "No changed work will begin … until the Change Request is … signed",
# which matches none of the obvious literals — so match the SHAPE (a negation,
# the word work, a starting verb) and the until/before-approval construction,
# rather than fixed wording.
_NO_WORK_RE = re.compile(
    r'\bno\s+(?:\w+\s+){0,3}work\b[^.]{0,160}?'
    r'\b(?:begin|commence|start|proceed|be\s+performed|be\s+undertaken)|'
    r'\b(?:not|shall not|will not|may not)\s+(?:be\s+)?'
    r'(?:commence|begin|start|proceed|performed|undertaken)\b[^.]{0,160}?'
    r'\b(?:approv|sign|authoris|authoriz)|'
    r'\b(?:until|before)\b[^.]{0,120}?\b(?:cr|change request)\b[^.]{0,120}?'
    r'\b(?:approv|sign|authoris|authoriz)|'
    r'\bonly\s+(?:after|upon|once)\b[^.]{0,120}?'
    r'\b(?:approv|sign|authoris|authoriz)',
    re.I)


def _client_patterns(client_name: str = "") -> List:
    """Owner/party patterns, extended with the engagement's real client name.

    A SOW written for Acme says "Acme", not "Customer" — matching only the
    generic words reports a one-sided contacts table or an unowned risk on a
    document that names the party perfectly well. The client name is passed in
    from the project record rather than guessed.
    """
    pats = list(_OWNER_PATTERNS)
    name = (client_name or "").strip()
    if name:
        # Match the distinctive words of the client name, so "Acme Corp"
        # also matches a row that just says "Acme". Two-plus characters only,
        # and skip corporate suffixes that would match half the document.
        stop = {"inc", "llc", "ltd", "limited", "corp", "corporation", "gmbh",
                "plc", "sa", "bv", "co", "company", "group", "holdings", "the"}
        for word in re.findall(r"[A-Za-z][A-Za-z0-9&-]{1,}", name):
            if word.lower() in stop:
                continue
            pats.append(re.compile(r'\b' + re.escape(word) + r'\b', re.I))
    return pats


def _md_tables(text: str) -> List[List[List[str]]]:
    """Every Markdown table in `text`, as a list of row-lists of cells."""
    tables, cur = [], []
    for line in (text or "").splitlines():
        ls = line.strip()
        if ls.startswith("|") and ls.count("|") >= 2:
            cells = [c.strip() for c in ls.strip("|").split("|")]
            if all(re.fullmatch(r':?-{2,}:?', c or "") for c in cells if c != ""):
                continue  # separator row
            cur.append(cells)
        elif cur:
            tables.append(cur); cur = []
    if cur:
        tables.append(cur)
    return [t for t in tables if len(t) >= 2]


def _flag(area: str, kind: str, severity: str, detail: str,
          section_id: Optional[str] = None, section_title: Optional[str] = None,
          story: str = "", evidence: str = "") -> Dict:
    return {"area": area, "area_label": _AREA_LABEL.get(area, area), "kind": kind,
            "severity": severity, "detail": detail, "section_id": section_id,
            "section_title": section_title, "story": story, "evidence": evidence[:180]}


def _locate(area: str, sections: List[Dict]) -> Optional[Dict]:
    ids, keywords, blocked = _AREAS[area]
    by_id = {s.get("id"): s for s in sections}
    for sid in ids:
        if sid in by_id:
            return by_id[sid]
    for s in sections:
        t = _norm(s.get("title", ""))
        if any(b in t for b in blocked):
            continue
        if any(k in t for k in keywords):
            return s
    return None


def _content_of(sec: Optional[Dict], sections: List[Dict],
                include_children: bool = False) -> str:
    """A section's drafted text. With include_children, appends every
    descendant's text too — a parent may carry only a framing sentence while
    the substance sits in its sub-sections."""
    if not sec:
        return ""
    parts = [(sec.get("content") or "")]
    if include_children:
        pid = sec.get("id") or ""
        for s in sections:
            sid = s.get("id") or ""
            if sid != pid and sid.startswith(pid + "."):
                parts.append(s.get("content") or "")
    return "\n".join(p for p in parts if p).strip()


def _found_boilerplate(text: str, phrases: List[str]) -> List[str]:
    low = text.lower()
    return [p for p in phrases if p in low]


# ─────────────────────────────────────────────────────────────────────────────
# US-04
# ─────────────────────────────────────────────────────────────────────────────

def _check_scope(sections: List[Dict], client_name: str = "") -> List[Dict]:
    owner_pats = _client_patterns(client_name)
    flags: List[Dict] = []

    for area in ("scope", "deliverables", "assumptions", "dependencies", "exclusions"):
        sec = _locate(area, sections)
        if sec is None:
            flags.append(_flag(area, "section_missing", "high", story="US-04",
                               detail=f"No {_AREA_LABEL[area]} section exists in this SOW. "
                                      f"All five scope areas must be present."))
            continue
        body = _content_of(sec, sections, include_children=(area == "scope"))
        if not body:
            flags.append(_flag(area, "section_empty", "high", story="US-04",
                               section_id=sec.get("id"), section_title=sec.get("title"),
                               detail=f"{_AREA_LABEL[area]} has no drafted content yet."))
            continue
        # A section that is only a placeholder is not "defined".
        stripped = _PLACEHOLDER_RE.sub("", body)
        if len(_WS.sub(" ", stripped).strip()) < 40:
            flags.append(_flag(area, "placeholder_only", "high", story="US-04",
                               section_id=sec.get("id"), section_title=sec.get("title"),
                               detail=f"{_AREA_LABEL[area]} is effectively empty — only "
                                      f"placeholders or a single fragment."))
            continue
        found = _found_boilerplate(body, _BOILERPLATE)
        if found:
            flags.append(_flag(area, "generic_boilerplate", "medium", story="US-04",
                               section_id=sec.get("id"), section_title=sec.get("title"),
                               evidence=", ".join(found),
                               detail=f"{_AREA_LABEL[area]} contains generic filler that "
                                      f"would read the same on any SOW: "
                                      f"{', '.join(repr(f) for f in found)}."))

    # Exclusions must actually exclude.
    exc = _locate("exclusions", sections)
    body = _content_of(exc, sections)
    if body:
        negative = re.search(
            r'\b(not included|not in scope|out of scope|excluded|excludes|will not|'
            r'shall not|does not include|no \w+ (?:is|are|will)\b)', body, re.I)
        if not negative:
            flags.append(_flag("exclusions", "exclusions_not_explicit", "high",
                               story="US-04", section_id=exc.get("id"),
                               section_title=exc.get("title"),
                               detail="Exclusions does not state what is NOT in scope in "
                                      "negative terms (\"not included\", \"excludes\", "
                                      "\"will not\"). An exclusions section that only "
                                      "describes in-scope work excludes nothing."))

    # Every dependency needs an owning party.
    dep = _locate("dependencies", sections)
    body = _content_of(dep, sections)
    if body:
        tables = _md_tables(body)
        if not tables:
            if not any(p.search(body) for p in owner_pats):
                flags.append(_flag("dependencies", "dependency_owner_missing", "high",
                                   story="US-04", section_id=dep.get("id"),
                                   section_title=dep.get("title"),
                                   detail="No dependency names an owning party. Each "
                                          "dependency must name Customer, Bristlecone or a "
                                          "named third party as owner."))
        else:
            for tbl in tables:
                header = [h.lower() for h in tbl[0]]
                col = next((i for i, h in enumerate(header)
                            if any(hint in h for hint in _OWNER_COL_HINTS)), None)
                if col is None:
                    flags.append(_flag("dependencies", "dependency_owner_column_missing",
                                       "high", story="US-04", section_id=dep.get("id"),
                                       section_title=dep.get("title"),
                                       evidence=" | ".join(tbl[0]),
                                       detail="The dependencies table has no owning-party "
                                              "column. Add \"Owning Party\" naming Customer, "
                                              "Bristlecone or a named third party."))
                    continue
                for row in tbl[1:]:
                    if col >= len(row):
                        continue
                    owner = row[col].strip()
                    label = row[0].strip() if row else ""
                    if not owner or not any(p.search(owner) for p in owner_pats):
                        flags.append(_flag(
                            "dependencies", "dependency_owner_unnamed", "high",
                            story="US-04", section_id=dep.get("id"),
                            section_title=dep.get("title"),
                            evidence=f"{label} → owner: {owner or '(blank)'}",
                            detail=f"Dependency \"{label or '(unnamed)'}\" does not name a "
                                   f"recognised owning party (Customer, Bristlecone or a "
                                   f"named third party); found {owner or 'nothing'!r}."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-05
# ─────────────────────────────────────────────────────────────────────────────

def _check_acceptance(sections: List[Dict]) -> List[Dict]:
    flags: List[Dict] = []
    sec = _locate("acceptance", sections)
    if sec is None:
        return [_flag("acceptance", "section_missing", "high", story="US-05",
                      detail="No Acceptance Criteria section exists in this SOW.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("acceptance", "section_empty", "high", story="US-05",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Acceptance Criteria has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")

    vague = _found_boilerplate(body, _VAGUE_ACCEPTANCE)
    if vague:
        flags.append(_flag("acceptance", "criteria_not_measurable", "high", story="US-05",
                           section_id=sid, section_title=stitle, evidence=", ".join(vague),
                           detail="Acceptance criteria use subjective wording that cannot be "
                                  "tested: " + ", ".join(repr(v) for v in vague) +
                                  ". Replace with a named test, threshold or document state."))

    if not _md_tables(body):
        flags.append(_flag("acceptance", "criteria_not_per_deliverable", "medium",
                           story="US-05", section_id=sid, section_title=stitle,
                           detail="No per-deliverable table found. Acceptance criteria must "
                                  "be defined per deliverable or milestone, not as one "
                                  "blanket statement."))

    window = _DAYS_RE.search(body)
    if not window:
        bare = _BARE_DAYS_RE.search(body)
        if bare:
            flags.append(_flag("acceptance", "review_window_unit_unclear", "medium",
                               story="US-05", section_id=sid, section_title=stitle,
                               evidence=bare.group(0),
                               detail=f"The review window ({bare.group(0)!r}) does not say "
                                      f"whether the days are business or calendar days."))
        else:
            flags.append(_flag("acceptance", "review_window_missing", "high", story="US-05",
                               section_id=sid, section_title=stitle,
                               detail="No review window is stated. It must be an explicit "
                                      "number of business or calendar days."))

    deemed = _DEEMED_RE.search(body)
    if not deemed:
        flags.append(_flag("acceptance", "deemed_acceptance_missing", "high", story="US-05",
                           section_id=sid, section_title=stitle,
                           detail="No deemed-acceptance clause. Without one a deliverable can "
                                  "sit unapproved indefinitely."))
    else:
        # Present, but does it actually fire on its own?
        span = body[max(0, deemed.start() - 240): deemed.end() + 240]
        if not _AUTO_RE.search(span) and not _DAYS_RE.search(span):
            flags.append(_flag("acceptance", "deemed_acceptance_not_automatic", "medium",
                               story="US-05", section_id=sid, section_title=stitle,
                               evidence=_WS.sub(" ", span)[:160],
                               detail="A deemed-acceptance clause is present but does not "
                                      "clearly trigger automatically on expiry of a stated "
                                      "window without written rejection."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-06
# ─────────────────────────────────────────────────────────────────────────────

def _check_change(sections: List[Dict]) -> List[Dict]:
    flags: List[Dict] = []
    sec = _locate("change", sections)
    # The Fixed Cost template puts CR detail under Commercials (13.6), so fall
    # back to any section whose title mentions change requests before giving up.
    if sec is None:
        for s in sections:
            if "change request" in _norm(s.get("title", "")):
                sec = s
                break
    if sec is None:
        return [_flag("change", "section_missing", "high", story="US-06",
                      detail="No Change Management / Change Request section exists.")]

    body = _content_of(sec, sections, include_children=True)
    # Pull in any sibling CR section elsewhere (e.g. 13.6 on Fixed Cost).
    for s in sections:
        if s is sec:
            continue
        if "change request" in _norm(s.get("title", "")) and s.get("content"):
            body += "\n" + s["content"]
    body = body.strip()

    if not body:
        return [_flag("change", "section_empty", "high", story="US-06",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="The Change Request section has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    low = body.lower()

    if not _md_tables(body) and not re.search(r'\b(step\s*1|raised|logged)\b', low):
        flags.append(_flag("change", "workflow_missing", "high", story="US-06",
                           section_id=sid, section_title=stitle,
                           detail="No CR workflow is defined. State the steps from CR raised "
                                  "through impact assessment, approval and closure."))

    if not re.search(r'\bapprov(?:er|ed by|al authority)\b|\bsign(?:ed|-off) by\b|'
                     r'\bauthori[sz]ed by\b|\bsponsor\b|\bengagement manager\b', low):
        flags.append(_flag("change", "approvers_missing", "high", story="US-06",
                           section_id=sid, section_title=stitle,
                           detail="The CR approvers are not named. Name the approving role "
                                  "on both the client and Bristlecone side."))

    if not (_DAYS_RE.search(body) or _BARE_DAYS_RE.search(body)
            or re.search(r'\bturnaround\b|\bsla\b|\bwithin \d+\b', low)):
        flags.append(_flag("change", "turnaround_missing", "high", story="US-06",
                           section_id=sid, section_title=stitle,
                           detail="No turnaround time for the CR process. Each step needs an "
                                  "explicit turnaround, in days."))

    has_cost = re.search(r'\bcost\b|\bcommercial\b|\bprice|\bpricing\b|\bfee\b|'
                         r'\bbudget\b|\beffort\b|\bvalue\b', low)
    has_time = re.search(r'\btimeline\b|\bschedule\b|\bmilestone\b|\bduration\b|'
                         r'\bdelivery date\b|\bplan\b', low)
    if not (has_cost and has_time):
        missing = []
        if not has_cost: missing.append("commercial/cost")
        if not has_time: missing.append("timeline/schedule")
        flags.append(_flag("change", "impact_assessment_incomplete", "high", story="US-06",
                           section_id=sid, section_title=stitle,
                           detail="Impact assessment must be mandatory and cover both "
                                  "commercial and timeline impact; this section does not "
                                  "address " + " or ".join(missing) + " impact."))
    elif not re.search(r'\bmandatory\b|\bmust\b|\brequired\b|\bcannot be approved without\b',
                       low):
        flags.append(_flag("change", "impact_assessment_not_mandatory", "medium",
                           story="US-06", section_id=sid, section_title=stitle,
                           detail="Impact assessment is described but not stated as mandatory "
                                  "for every CR."))

    if not _NO_WORK_RE.search(body):
        flags.append(_flag("change", "no_work_rule_missing", "high", story="US-06",
                           section_id=sid, section_title=stitle,
                           detail="The SOW does not state that no work begins on an "
                                  "unapproved change."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────

def run_checks(sections: List[Dict], client_name: str = "") -> Dict:
    """Run every US-04/05/06 check over a project's drafted sections."""
    drafted = [s for s in sections if s.get("has_own_content", True)]
    flags = (_check_scope(drafted, client_name) + _check_acceptance(drafted)
             + _check_change(drafted) + _check_commercials(drafted)
             + _check_governance(drafted, client_name)
             + _check_risks(drafted, client_name) + _check_kpis(drafted)
             + _check_onboarding(drafted) + _check_assets(drafted)
             + _check_term(drafted) + _check_vendor_alignment(drafted)
             + _check_vendor_compliance(drafted))
    order = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: (order.get(f["severity"], 9), f["story"], f["area"]))
    by_story: Dict[str, int] = {}
    for f in flags:
        by_story[f["story"]] = by_story.get(f["story"], 0) + 1
    return {
        "flag_count": len(flags),
        "high_count": sum(1 for f in flags if f["severity"] == "high"),
        "by_story": by_story,
        "flags": flags,
    }


# ─────────────────────────────────────────────────────────────────────────────
# US-07 — complete commercial terms
# ─────────────────────────────────────────────────────────────────────────────

_CREDIT_RE = re.compile(
    r'\b(?:net|within|payable within|due within|credit period of)\s*'
    r'(?:\d{1,3}|thirty|sixty|forty[\s\-]?five|ninety|fifteen)\b[^.\n]{0,30}?\bdays?\b'
    r'|\b(?:\d{1,3})\s*(?:\(\s*\w+\s*\))?\s*days?\b[^.\n]{0,40}?'
    r'\b(?:from (?:the )?invoice|of invoice|after invoice|invoice date)\b',
    re.I)
_INTEREST_RE = re.compile(
    r'\binterest\b|\blate[\s\-]payment\b|\boverdue\b|\bpenalt|\bsuspend\b|'
    r'\bwithhold (?:further )?(?:services|delivery)\b|\bdefault\b', re.I)
_CURRENCY_RE = re.compile(
    r'\b(?:USD|EUR|GBP|INR|AUD|CAD|SGD|JPY|CHF|AED)\b|[$€£₹]|'
    r'\b(?:us dollars?|euros?|pounds? sterling|rupees?)\b', re.I)
_TAX_RE = re.compile(r'\bVAT\b|\bGST\b|\bsales tax\b|\btaxes?\b|\bwithholding\b|'
                     r'\bexclusive of tax\b|\bservice tax\b', re.I)
_EXPENSE_RE = re.compile(r'\bexpenses?\b|\btravel\b|\bout[\s\-]of[\s\-]pocket\b|'
                         r'\bper[\s\-]diem\b|\breimburs', re.I)
_BILLING_MODEL_RE = re.compile(
    r'\btime (?:and|&) materials?\b|\bT&M\b|\bfixed[\s\-]?(?:cost|price|fee)\b|'
    r'\bmilestone[\s\-]based\b|\bhybrid\b|\bretainer\b|\bfixed monthly\b', re.I)


def _check_commercials(sections: List[Dict]) -> List[Dict]:
    sec = _locate("commercials", sections)
    if sec is None:
        return [_flag("commercials", "section_missing", "high", story="US-07",
                      detail="No Commercials section exists in this SOW.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("commercials", "section_empty", "high", story="US-07",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Commercials has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []

    def need(pattern, kind, detail, severity="high"):
        if not pattern.search(body):
            flags.append(_flag("commercials", kind, severity, story="US-07",
                               section_id=sid, section_title=stitle, detail=detail))

    need(_BILLING_MODEL_RE, "billing_model_missing",
         "The billing model is not stated. Say explicitly whether this is Time & Materials, "
         "Fixed Cost, milestone-based or hybrid.")
    need(_CREDIT_RE, "credit_period_missing",
         "No credit period in days. Payment terms must give an explicit number of days from "
         "invoice date (e.g. \"net thirty (30) days\").")
    need(_INTEREST_RE, "delayed_payment_clause_missing",
         "No delayed-payment clause. State the interest rate on overdue amounts or another "
         "consequence (suspension of services after notice).")
    need(_CURRENCY_RE, "currency_missing",
         "No invoicing currency is stated.")
    need(_TAX_RE, "tax_treatment_missing",
         "Tax treatment is not stated — say whether amounts are exclusive of VAT/GST/"
         "withholding and who bears them.")
    need(_EXPENSE_RE, "expense_treatment_missing",
         "Expense treatment is not stated — say whether travel and out-of-pocket expenses are "
         "billed in addition to fees or included in them.")
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-08 — governance and escalation
# ─────────────────────────────────────────────────────────────────────────────

_CADENCE_RE = re.compile(
    r'\b(?:daily|weekly|fortnight(?:ly)?|bi[\s\-]?weekly|monthly|quarterly|'
    r'twice (?:a|per) week|every \d+ (?:days?|weeks?))\b', re.I)
_RESPONSE_TIME_RE = re.compile(
    r'\b\d{1,3}\s*(?:\(\s*\w+\s*\))?\s*(?:business |working |calendar )?'
    r'(?:hours?|hrs?|days?)\b|\bsame day\b|\bimmediate(?:ly)?\b', re.I)
_LEVEL_RE = re.compile(r'\b(?:level|l|tier)\s*[\-]?\s*([123])\b', re.I)
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+|<\s*CONFIRM[^>]*(?:email|contact)[^>]*>', re.I)
_PARTY_RE = re.compile(r'\bcustomer\b|\bclient\b|\bbristlecone\b', re.I)


def _party_regex(client_name: str = ""):
    """_PARTY_RE widened with the engagement's actual client name, so a SOW
    that says "Acme" rather than "Customer" still reads as naming a party."""
    return re.compile("|".join(p.pattern for p in _client_patterns(client_name)), re.I)


def _check_governance(sections: List[Dict], client_name: str = "") -> List[Dict]:
    party_re = _party_regex(client_name)
    flags: List[Dict] = []

    gov = _locate("governance", sections)
    gov_body = _content_of(gov, sections, include_children=True) if gov else ""
    if not gov_body:
        flags.append(_flag("governance", "section_missing" if not gov else "section_empty",
                           "high", story="US-08",
                           section_id=gov.get("id") if gov else None,
                           section_title=gov.get("title") if gov else None,
                           detail="No governance content. Governance forums, their cadence "
                                  "and their participants must be listed."))
    else:
        gid, gtitle = gov.get("id"), gov.get("title")
        if not _CADENCE_RE.search(gov_body):
            flags.append(_flag("governance", "forum_cadence_missing", "high", story="US-08",
                               section_id=gid, section_title=gtitle,
                               detail="No meeting cadence is stated for any governance forum "
                                      "(daily / weekly / monthly …)."))
        if not party_re.search(gov_body):
            flags.append(_flag("governance", "forum_participants_missing", "high",
                               story="US-08", section_id=gid, section_title=gtitle,
                               detail="Governance forums do not name participants from both "
                                      "parties."))

    esc = _locate("escalation", sections)
    esc_body = _content_of(esc, sections) if esc else ""
    # An escalation ladder may legitimately live inside the governance section.
    if not esc_body and gov_body and "escalat" in gov_body.lower():
        esc, esc_body = gov, gov_body
    if not esc_body:
        flags.append(_flag("escalation", "escalation_matrix_missing", "high", story="US-08",
                           detail="No escalation matrix. Levels 1 to 3 must each name roles "
                                  "on both sides with a response timeline."))
    else:
        eid, etitle = esc.get("id"), esc.get("title")
        levels = {m.group(1) for m in _LEVEL_RE.finditer(esc_body)}
        missing = sorted({"1", "2", "3"} - levels)
        if missing:
            flags.append(_flag("escalation", "escalation_levels_incomplete", "high",
                               story="US-08", section_id=eid, section_title=etitle,
                               evidence="found levels: " + (", ".join(sorted(levels)) or "none"),
                               detail="Escalation levels " + ", ".join(missing) +
                                      " are not defined. All three levels are required."))
        if not _RESPONSE_TIME_RE.search(esc_body):
            flags.append(_flag("escalation", "escalation_response_time_missing", "high",
                               story="US-08", section_id=eid, section_title=etitle,
                               detail="No response timeline on the escalation levels — each "
                                      "needs a time in hours or business days."))
        if not party_re.search(esc_body):
            flags.append(_flag("escalation", "escalation_roles_missing", "high", story="US-08",
                               section_id=eid, section_title=etitle,
                               detail="Escalation levels do not name roles on both the "
                                      "customer and Bristlecone side."))

    con = _locate("contacts", sections)
    con_body = _content_of(con, sections) if con else ""
    if not con_body and gov_body and _EMAIL_RE.search(gov_body):
        con, con_body = gov, gov_body
    if not con_body:
        flags.append(_flag("contacts", "contacts_missing", "high", story="US-08",
                           detail="No contact details are maintained. Both parties need named "
                                  "contacts (a <CONFIRM: …> placeholder is acceptable, an "
                                  "absent section is not)."))
    else:
        cid, ctitle = con.get("id"), con.get("title")
        if not _EMAIL_RE.search(con_body):
            flags.append(_flag("contacts", "contact_details_missing", "medium", story="US-08",
                               section_id=cid, section_title=ctitle,
                               detail="Key Contacts lists no email or contact route for "
                                      "either party."))
        has_bristlecone = re.search(r'\bbristlecone\b', con_body, re.I)
        has_client = any(p.search(con_body) for p in _client_patterns(client_name)
                         if "bristlecone" not in p.pattern.lower()
                         and "supplier" not in p.pattern.lower()
                         and "vendor" not in p.pattern.lower())
        if not has_bristlecone or not has_client:
            flags.append(_flag("contacts", "contacts_one_sided", "medium", story="US-08",
                               section_id=cid, section_title=ctitle,
                               detail="Contacts are not maintained for BOTH parties."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-09 — risk register with ownership
# ─────────────────────────────────────────────────────────────────────────────

_MITIGATION_WEAK = ["monitor", "monitor closely", "keep under review", "manage carefully",
                    "as needed", "tbd", "n/a", "-"]


def _check_risks(sections: List[Dict], client_name: str = "") -> List[Dict]:
    owner_pats = _client_patterns(client_name)
    sec = _locate("risks", sections)
    if sec is None:
        return [_flag("risks", "section_missing", "high", story="US-09",
                      detail="No Risks section exists in this SOW.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("risks", "section_empty", "high", story="US-09",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="The risk register has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []
    tables = _md_tables(body)
    if not tables:
        return [_flag("risks", "risk_table_missing", "high", story="US-09",
                      section_id=sid, section_title=stitle,
                      detail="Risks are not tabulated. Each risk needs impact, likelihood, "
                             "mitigation and a named owner.")]

    for tbl in tables:
        header = [h.lower() for h in tbl[0]]

        def col(*hints):
            return next((i for i, h in enumerate(header)
                         if any(x in h for x in hints)), None)

        c_like = col("likelihood", "probability")
        c_imp = col("impact", "severity", "consequence")
        c_mit = col("mitigation", "response", "treatment", "action")
        c_own = col("owner", "owning", "responsible", "accountable")

        for idx, kind, what in (
                (c_like, "risk_likelihood_missing", "a Likelihood column"),
                (c_imp, "risk_impact_missing", "an Impact column"),
                (c_mit, "risk_mitigation_missing", "a Mitigation column"),
                (c_own, "risk_owner_column_missing", "a Risk Owner column")):
            if idx is None:
                flags.append(_flag("risks", kind, "high", story="US-09", section_id=sid,
                                   section_title=stitle, evidence=" | ".join(tbl[0]),
                                   detail=f"The risk table has no {what}."))

        for row in tbl[1:]:
            name = row[0].strip() if row else ""
            if c_own is not None and c_own < len(row):
                owner = row[c_own].strip()
                has_party = any(p.search(owner) for p in owner_pats)
                # "party and role" — a bare party name is not an owner.
                has_role = bool(re.search(r'[\-—–:,]|\b(manager|lead|owner|director|'
                                          r'sponsor|architect|head|officer|analyst|'
                                          r'pm|smes?)\b', owner, re.I))
                if not owner or not has_party:
                    flags.append(_flag("risks", "risk_owner_unnamed", "high", story="US-09",
                                       section_id=sid, section_title=stitle,
                                       evidence=f"{name} -> owner: {owner or '(blank)'}",
                                       detail=f"Risk \"{name or '(unnamed)'}\" has no owning "
                                              f"party named."))
                elif not has_role:
                    flags.append(_flag("risks", "risk_owner_role_missing", "medium",
                                       story="US-09", section_id=sid, section_title=stitle,
                                       evidence=f"{name} -> owner: {owner}",
                                       detail=f"Risk \"{name or '(unnamed)'}\" names a party "
                                              f"({owner}) but no role."))
            if c_mit is not None and c_mit < len(row):
                mit = row[c_mit].strip().lower()
                if mit and mit in _MITIGATION_WEAK:
                    flags.append(_flag("risks", "risk_mitigation_weak", "medium",
                                       story="US-09", section_id=sid, section_title=stitle,
                                       evidence=f"{name} -> {mit}",
                                       detail=f"Risk \"{name}\" has no real mitigation — "
                                              f"{mit!r} is not an action."))
            for label, idx in (("Likelihood", c_like), ("Impact", c_imp)):
                if idx is not None and idx < len(row) and not row[idx].strip():
                    flags.append(_flag("risks", "risk_rating_blank", "medium", story="US-09",
                                       section_id=sid, section_title=stitle,
                                       evidence=f"{name} -> {label} blank",
                                       detail=f"Risk \"{name}\" has no {label} rating."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-10 — KPIs, SLAs and success metrics
# ─────────────────────────────────────────────────────────────────────────────

_TARGET_RE = re.compile(r'\d+\s*%|\b\d+(?:\.\d+)?\s*(?:hours?|days?|hrs?|business days?|'
                        r'defects?|incidents?|per|/)\b|\b100%|\bzero\b|\bnil\b', re.I)
_METHOD_HINTS = ["measurement method", "method", "how measured", "calculation", "formula"]
_SOURCE_HINTS = ["data source", "source", "system of record", "evidence", "report source"]
_NO_CREDITS_RE = re.compile(
    r'no service credits|without service credits|no financial penalt|'
    r'no penalt|not applicable|no sla|does not carry (?:any )?service credit', re.I)
_BREACH_RE = re.compile(
    r'service credit|remediation plan|penalt|breach[^.\n]{0,60}?(?:escalat|credit|remedy)|'
    r'escalat[^.\n]{0,60}?breach|root cause analysis', re.I)


def _check_kpis(sections: List[Dict]) -> List[Dict]:
    sec = _locate("kpis", sections)
    if sec is None:
        return [_flag("kpis", "section_missing", "high", story="US-10",
                      detail="No Performance Reporting / KPI section exists in this SOW.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("kpis", "section_empty", "high", story="US-10",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="KPIs / SLAs have no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []
    tables = _md_tables(body)

    if not tables:
        flags.append(_flag("kpis", "metrics_not_tabulated", "high", story="US-10",
                           section_id=sid, section_title=stitle,
                           detail="Metrics are not tabulated with targets and frequency."))
    else:
        joined_header = " ".join(h.lower() for t in tables for h in t[0])
        if not _TARGET_RE.search(body):
            flags.append(_flag("kpis", "targets_not_measurable", "high", story="US-10",
                               section_id=sid, section_title=stitle,
                               detail="No numeric or binary target found. Metrics must have "
                                      "measurable targets, not \"good performance\"."))
        if "frequen" not in joined_header and not _CADENCE_RE.search(body):
            flags.append(_flag("kpis", "measurement_frequency_missing", "high", story="US-10",
                               section_id=sid, section_title=stitle,
                               detail="No measurement/reporting frequency is stated."))
        if not any(h in joined_header for h in _METHOD_HINTS):
            flags.append(_flag("kpis", "measurement_method_missing", "high", story="US-10",
                               section_id=sid, section_title=stitle,
                               evidence=joined_header[:120],
                               detail="No measurement method is defined — say how each metric "
                                      "is calculated."))
        if not any(h in joined_header for h in _SOURCE_HINTS):
            flags.append(_flag("kpis", "data_source_missing", "high", story="US-10",
                               section_id=sid, section_title=stitle,
                               evidence=joined_header[:120],
                               detail="No data source is defined — say which system or "
                                      "artefact each metric is measured from."))

    if not (_BREACH_RE.search(body) or _NO_CREDITS_RE.search(body)):
        flags.append(_flag("kpis", "breach_consequence_missing", "high", story="US-10",
                           section_id=sid, section_title=stitle,
                           detail="The consequence of an SLA breach is neither stated nor "
                                  "explicitly ruled out. Say what happens on breach, or state "
                                  "plainly that no service credits apply."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-11 — onboarding and access provisioning timelines
# ─────────────────────────────────────────────────────────────────────────────

_ACCESS_ITEMS = {
    "vpn": re.compile(r'\bvpn\b|\bremote access\b', re.I),
    "licence": re.compile(r'\blicen[cs]e', re.I),
    "access": re.compile(r'\baccess\b|\baccount\b|\bcredential', re.I),
}
_BILLING_IMPACT_RE = re.compile(
    r'\bbillab|\bcharge|\binvoic|\bbilling\b|\bcost\b|\bfee\b|\bidle\b|'
    r'\bstand[\s\-]?by\b|\bnon[\s\-]?productive\b', re.I)
_SCHEDULE_IMPACT_RE = re.compile(
    r'\bschedul|\bmilestone|\btimeline|\bdelivery date|\bslip|\bshift|'
    r'\bextend|\bdelay[^.\n]{0,60}?\b(?:date|plan|milestone)', re.I)


def _check_onboarding(sections: List[Dict]) -> List[Dict]:
    sec = _locate("onboarding", sections)
    if sec is None:
        return [_flag("onboarding", "section_missing", "high", story="US-11",
                      detail="No Onboarding & Access Provisioning section exists.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("onboarding", "section_empty", "high", story="US-11",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Onboarding & Access Provisioning has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []

    if not _md_tables(body):
        flags.append(_flag("onboarding", "onboarding_steps_not_tabulated", "medium",
                           story="US-11", section_id=sid, section_title=stitle,
                           detail="Onboarding steps are not tabulated with owners and "
                                  "durations."))
    if not (_DAYS_RE.search(body) or _BARE_DAYS_RE.search(body)
            or re.search(r'\bweeks?\b', body, re.I)):
        flags.append(_flag("onboarding", "onboarding_duration_missing", "high", story="US-11",
                           section_id=sid, section_title=stitle,
                           detail="No expected duration for the onboarding steps. Each step "
                                  "needs a duration in days or weeks."))

    missing = [k for k, rx in _ACCESS_ITEMS.items() if not rx.search(body)]
    if missing:
        pretty = {"vpn": "VPN / remote access", "licence": "licences",
                  "access": "system access or accounts"}
        flags.append(_flag("onboarding", "customer_obligations_incomplete", "high",
                           story="US-11", section_id=sid, section_title=stitle,
                           evidence="missing: " + ", ".join(missing),
                           detail="Customer obligations do not cover "
                                  + ", ".join(pretty[m] for m in missing) +
                                  ". All of access, licences and VPN must be listed with "
                                  "timelines."))

    has_sched = bool(_SCHEDULE_IMPACT_RE.search(body))
    has_bill = bool(_BILLING_IMPACT_RE.search(body))
    if not (has_sched and has_bill):
        lack = []
        if not has_sched:
            lack.append("schedule")
        if not has_bill:
            lack.append("billing")
        flags.append(_flag("onboarding", "delay_impact_missing", "high", story="US-11",
                           section_id=sid, section_title=stitle,
                           detail="The impact of access delays on " + " and ".join(lack) +
                                  " is not stated. Say what a customer-side access delay does "
                                  "to milestone dates and to chargeability."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-12 — asset management and return at closure
# ─────────────────────────────────────────────────────────────────────────────

_ASSET_ISSUE_RE = re.compile(
    r'\bissu(?:e|ed|ance)\b|\ballocat|\bregister\b|\btrack(?:ed|ing)?\b|'
    r'\binventory\b|\basset tag\b', re.I)
_ASSET_RETURN_RE = re.compile(r'\breturn|\bsurrender|\bhand(?:ed)?\s*back\b|\brecover', re.I)
_CLOSURE_RE = re.compile(r'\bclosure\b|\bcompletion\b|\bend of (?:the )?(?:project|engagement)\b|'
                         r'\btermination\b|\bexit\b', re.I)
_ROLLOVER_RE = re.compile(r'\brollover\b|\broll[\s\-]off\b|\breplacement\b|\breassign|'
                          r'\bleaves? the (?:project|engagement)\b|\bexits? the (?:project|team)\b|'
                          r'\bresource change\b|\bdemobilis', re.I)
_LOSS_RE = re.compile(r'\bloss\b|\blost\b|\bdamage\b|\bnon[\s\-]return\b|\bnot returned\b|'
                      r'\bfail(?:ure|s)? to return\b|\breplacement (?:value|cost)\b|'
                      r'\brecover the cost\b|\bdeduct', re.I)


def _check_assets(sections: List[Dict]) -> List[Dict]:
    sec = _locate("assets", sections)
    if sec is None:
        return [_flag("assets", "section_missing", "high", story="US-12",
                      detail="No Asset Management & Return section exists.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("assets", "section_empty", "high", story="US-12",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Asset Management & Return has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []

    if not _ASSET_ISSUE_RE.search(body):
        flags.append(_flag("assets", "issuance_process_missing", "high", story="US-12",
                           section_id=sid, section_title=stitle,
                           detail="No asset issuance or tracking process is described."))
    if not _ASSET_RETURN_RE.search(body):
        flags.append(_flag("assets", "return_obligation_missing", "high", story="US-12",
                           section_id=sid, section_title=stitle,
                           detail="No return obligation is stated for issued assets."))
    else:
        if not _CLOSURE_RE.search(body):
            flags.append(_flag("assets", "return_at_closure_missing", "high", story="US-12",
                               section_id=sid, section_title=stitle,
                               detail="Return obligations do not cover project closure."))
        if not _ROLLOVER_RE.search(body):
            flags.append(_flag("assets", "return_on_rollover_missing", "high", story="US-12",
                               section_id=sid, section_title=stitle,
                               detail="Return obligations do not cover resource rollover — an "
                                      "individual leaving mid-engagement must return kit too."))
        if not (_DAYS_RE.search(body) or _BARE_DAYS_RE.search(body)):
            flags.append(_flag("assets", "return_timeline_missing", "high", story="US-12",
                               section_id=sid, section_title=stitle,
                               detail="No return timeline in days is stated."))
    if not _LOSS_RE.search(body):
        flags.append(_flag("assets", "loss_accountability_missing", "high", story="US-12",
                           section_id=sid, section_title=stitle,
                           detail="Accountability for loss or non-return is not stated — say "
                                  "who bears the cost and how it is recovered."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-13 — term and termination
# ─────────────────────────────────────────────────────────────────────────────

_START_RE = re.compile(r'\bstart date\b|\beffective date\b|\bcommence(?:s|ment)?\b|'
                       r'\bbegins? on\b|\bfrom \d{4}-\d{2}-\d{2}\b', re.I)
_DURATION_RE = re.compile(r'\bduration\b|\bend date\b|\bexpir|\bterm of\b|'
                          r'\b\d{1,3}\s*(?:months?|weeks?|years?)\b|\buntil\b', re.I)
_RENEWAL_RE = re.compile(r'\brenew|\bextend|\bextension\b|\bauto[\s\-]?renew|'
                         r'\bno renewal\b|\bnot (?:be )?renewed\b', re.I)
_CONVENIENCE_RE = re.compile(r'\bfor convenience\b|\bwithout cause\b|\bat any time\b', re.I)
_CAUSE_RE = re.compile(r'\bfor cause\b|\bmaterial breach\b|\binsolven|\bdefault\b|'
                       r'\bfraud\b|\bbreach of\b', re.I)
_EXIT_PARTS = {
    "knowledge transfer": re.compile(r'\bknowledge transfer\b|\bhandover\b|\bhand[\s\-]over\b|'
                                     r'\btransition (?:out|assistance|support)\b', re.I),
    "data return or deletion": re.compile(r'\bdata\b[^.\n]{0,60}?\b(?:return|deletion|delete|'
                                          r'destroy|purge)\b|\breturn[^.\n]{0,40}?\bdata\b', re.I),
    "asset return": re.compile(r'\basset[^.\n]{0,40}?\breturn|\breturn[^.\n]{0,40}?\basset|'
                               r'\bequipment[^.\n]{0,40}?\breturn', re.I),
    "final settlement": re.compile(r'\bfinal settlement\b|\bfinal invoice\b|\bfinal payment\b|'
                                   r'\breconcil|\boutstanding amounts?\b', re.I),
}


def _check_term(sections: List[Dict]) -> List[Dict]:
    sec = _locate("termination", sections)
    if sec is None:
        return [_flag("termination", "section_missing", "high", story="US-13",
                      detail="No Term & Termination section exists.")]
    body = _content_of(sec, sections, include_children=True)
    # The term itself is often stated in Agreement Details; accept it there.
    agreement = _locate("agreement", sections)
    agreement_body = _content_of(agreement, sections, include_children=True) if agreement else ""
    term_body = (body + "\n" + agreement_body).strip()

    if not body:
        return [_flag("termination", "section_empty", "high", story="US-13",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Term & Termination has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []

    for rx, kind, what in (
            (_START_RE, "term_start_missing", "a start or effective date"),
            (_DURATION_RE, "term_duration_missing", "a duration or end date"),
            (_RENEWAL_RE, "renewal_mechanism_missing",
             "a renewal or extension mechanism (or an explicit statement that there is none)")):
        if not rx.search(term_body):
            flags.append(_flag("termination", kind, "high", story="US-13", section_id=sid,
                               section_title=stitle,
                               detail="The term does not state " + what + "."))

    for rx, kind, what in ((_CONVENIENCE_RE, "termination_convenience_missing",
                            "Termination for convenience"),
                           (_CAUSE_RE, "termination_cause_missing", "Termination for cause")):
        if not rx.search(body):
            flags.append(_flag("termination", kind, "high", story="US-13", section_id=sid,
                               section_title=stitle,
                               detail=what + " is not covered."))

    if not (_DAYS_RE.search(body) or _BARE_DAYS_RE.search(body)):
        flags.append(_flag("termination", "notice_period_missing", "high", story="US-13",
                           section_id=sid, section_title=stitle,
                           detail="No notice period in days is stated for either termination "
                                  "route."))

    missing = [name for name, rx in _EXIT_PARTS.items() if not rx.search(body)]
    if missing:
        flags.append(_flag("termination", "exit_obligations_incomplete", "high", story="US-13",
                           section_id=sid, section_title=stitle,
                           evidence="missing: " + "; ".join(missing),
                           detail="Exit obligations do not cover " + "; ".join(missing) +
                                  ". All four are required: knowledge transfer, data return "
                                  "or deletion, asset return, and final settlement."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-14 — back-to-back vendor alignment
# ─────────────────────────────────────────────────────────────────────────────

_MIRROR_RE = re.compile(r'\bmirror|\bback[\s\-]to[\s\-]back\b|\bflow(?:ed|s)?[\s\-]down\b|'
                        r'\bpass(?:ed)?[\s\-]through\b|\balign(?:ed|ment)? with\b|'
                        r'\bno less onerous\b|\bequivalent to\b', re.I)
_VENDOR_SLA_RE = re.compile(r'\bslas?\b|\bservice[\s\-]levels?\b|\bkpis?\b', re.I)
_VENDOR_ACCEPT_RE = re.compile(r'\bacceptance\b', re.I)
_TIMESHEET_SUBSTITUTE_RE = re.compile(
    r'\bnot\b[^.\n]{0,80}?\btimesheet\b|\btimesheet\b[^.\n]{0,80}?\bnot\b|'
    r'\bdeliverable[\s\-]based\b|\brather than (?:a )?timesheet\b', re.I)
_VENDOR_PAYMENT_RE = re.compile(
    r'\bpayment terms?\b|\bcredit period\b|\bnet\s*\d+\b|\bpay(?:able|ment) (?:within|after)\b',
    re.I)


def _check_vendor_alignment(sections: List[Dict]) -> List[Dict]:
    sec = _locate("vendor_alignment", sections)
    if sec is None:
        return [_flag("vendor_alignment", "section_missing", "high", story="US-14",
                      detail="No Subcontractor & Vendor Alignment section exists.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("vendor_alignment", "section_empty", "high", story="US-14",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Subcontractor & Vendor Alignment has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []

    if not _MIRROR_RE.search(body):
        flags.append(_flag("vendor_alignment", "mirroring_not_stated", "high", story="US-14",
                           section_id=sid, section_title=stitle,
                           detail="The SOW does not state that vendor scope, SLAs and "
                                  "acceptance terms mirror the customer commitments."))
    if not _VENDOR_SLA_RE.search(body):
        flags.append(_flag("vendor_alignment", "vendor_sla_missing", "high", story="US-14",
                           section_id=sid, section_title=stitle,
                           detail="Vendor SLAs are not addressed."))
    if not _VENDOR_ACCEPT_RE.search(body):
        flags.append(_flag("vendor_alignment", "vendor_acceptance_missing", "high",
                           story="US-14", section_id=sid, section_title=stitle,
                           detail="The vendor acceptance basis is not addressed."))
    elif not _TIMESHEET_SUBSTITUTE_RE.search(body):
        flags.append(_flag("vendor_alignment", "acceptance_basis_unqualified", "medium",
                           story="US-14", section_id=sid, section_title=stitle,
                           detail="Acceptance is mentioned but the SOW does not say that "
                                  "deliverable-based acceptance is not replaced by timesheet "
                                  "approval where deliverables govern."))
    if not _VENDOR_PAYMENT_RE.search(body):
        flags.append(_flag("vendor_alignment", "vendor_payment_terms_missing", "high",
                           story="US-14", section_id=sid, section_title=stitle,
                           detail="Vendor payment terms are not stated, so compatibility with "
                                  "the customer credit period cannot be judged."))
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# US-15 — contractor and vendor compliance
# ─────────────────────────────────────────────────────────────────────────────

_COMPLIANCE_REGIMES = {
    "POSH": re.compile(r'\bPOSH\b|prevention of sexual harassment', re.I),
    "Code of Conduct": re.compile(r'\bcode of conduct\b|\bCoC\b', re.I),
    "ISMS": re.compile(r'\bISMS\b|information security management', re.I),
    "PIMS": re.compile(r'\bPIMS\b|privacy information management', re.I),
}
_BINDING_RE = re.compile(r'\bbinding\b|\bcontractually\b|\bshall comply\b|\bmust comply\b|'
                         r'\brequired to comply\b|\bflow(?:ed|s)?[\s\-]down\b', re.I)
_EVIDENCE_RE = re.compile(r'\bevidence\b|\backnowledge?ment\b|\backnowledge\b|\bsigned\b|'
                          r'\btraining (?:record|completion|certificate)\b|\battestation\b|'
                          r'\bcertificat', re.I)
_BEFORE_ACCESS_RE = re.compile(r'\bbefore\b[^.\n]{0,80}?\baccess\b|'
                               r'\bprior to\b[^.\n]{0,80}?\baccess\b|'
                               r'\baccess\b[^.\n]{0,80}?\b(?:only after|is granted only|'
                               r'conditional (?:up)?on)\b|\bpre[\s\-]?requisite\b', re.I)
_AUDIT_RE = re.compile(r'\baudit\b|\binspect', re.I)
_CONSEQUENCE_RE = re.compile(r'\brevoke|\brevocation\b|\bremoval\b|\bremove\b|\bterminat|'
                             r'\bsuspend|\bmaterial breach\b|\bdebar', re.I)


def _check_vendor_compliance(sections: List[Dict]) -> List[Dict]:
    sec = _locate("vendor_compliance", sections)
    if sec is None:
        return [_flag("vendor_compliance", "section_missing", "high", story="US-15",
                      detail="No Contractor & Vendor Compliance section exists.")]
    body = _content_of(sec, sections, include_children=True)
    if not body:
        return [_flag("vendor_compliance", "section_empty", "high", story="US-15",
                      section_id=sec.get("id"), section_title=sec.get("title"),
                      detail="Contractor & Vendor Compliance has no drafted content yet.")]

    sid, stitle = sec.get("id"), sec.get("title")
    flags: List[Dict] = []

    missing = [name for name, rx in _COMPLIANCE_REGIMES.items() if not rx.search(body)]
    if missing:
        flags.append(_flag("vendor_compliance", "regimes_missing", "high", story="US-15",
                           section_id=sid, section_title=stitle,
                           evidence="missing: " + ", ".join(missing),
                           detail="These mandatory regimes are not named: "
                                  + ", ".join(missing) +
                                  ". All four (POSH, Code of Conduct, ISMS, PIMS) must be "
                                  "contractually binding on vendors and their personnel."))
    if not _BINDING_RE.search(body):
        flags.append(_flag("vendor_compliance", "not_stated_binding", "high", story="US-15",
                           section_id=sid, section_title=stitle,
                           detail="The SOW does not state that these obligations are "
                                  "contractually binding on the vendor and its personnel."))
    if not _EVIDENCE_RE.search(body):
        flags.append(_flag("vendor_compliance", "evidence_requirement_missing", "high",
                           story="US-15", section_id=sid, section_title=stitle,
                           detail="No requirement for evidence of acknowledgement or training."))
    elif not _BEFORE_ACCESS_RE.search(body):
        flags.append(_flag("vendor_compliance", "evidence_not_before_access", "high",
                           story="US-15", section_id=sid, section_title=stitle,
                           detail="Evidence is required but not as a precondition of access. "
                                  "It must be provided BEFORE access is granted."))
    if not _CONSEQUENCE_RE.search(body):
        flags.append(_flag("vendor_compliance", "consequences_missing", "high", story="US-15",
                           section_id=sid, section_title=stitle,
                           detail="Consequences of non-compliance are not stated."))
    if not _AUDIT_RE.search(body):
        flags.append(_flag("vendor_compliance", "audit_rights_missing", "high", story="US-15",
                           section_id=sid, section_title=stitle,
                           detail="No audit rights over vendor compliance records are stated."))
    return flags
