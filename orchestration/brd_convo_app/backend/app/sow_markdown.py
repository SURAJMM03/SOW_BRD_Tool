"""
sow_markdown.py — shared inline-markdown tokenizer for SOW content.

Generated section content is plain Markdown-ish text (the generation prompt
in sow_section_routes.py explicitly tells the model to "use bold text
instead of a heading" for emphasis, and PLACEHOLDER_RULES' own examples use
backtick-formatted illustrations that the model imitates). Neither the
docx export path (sow_workflow.py) nor the HTML preview path
(sow_section_routes.py) interpreted any of this — `**bold**` and stray
backticks showed up literally in both. This module is the single tokenizer
both paths use, so Preview and the exported Word doc agree on what "the
document" looks like.
"""
from __future__ import annotations

import re
from typing import List, Tuple

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def split_bold_segments(text: str) -> List[Tuple[str, bool]]:
    """Strip stray backticks (code-span markers the model imitates from the
    prompt's own instruction text — not meaningful formatting here, so they
    are just noise to drop) and split the remaining text on `**bold**`
    spans. Returns a list of (text_segment, is_bold) pairs, in order;
    concatenating the text_segments reproduces the original (backtick-
    stripped) string exactly."""
    text = text.replace("`", "")
    if "**" not in text:
        return [(text, False)] if text else []

    segments: List[Tuple[str, bool]] = []
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], False))
        if m.group(1):
            segments.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], False))
    return segments
