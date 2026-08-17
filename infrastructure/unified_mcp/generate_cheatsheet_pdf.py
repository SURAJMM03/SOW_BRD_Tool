#!/usr/bin/env python3
"""Generate Kinaxis MCP Server cheat sheet PDF using reportlab."""

from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, KeepTogether
)
from reportlab.platypus.flowables import HRFlowable
from reportlab.lib.enums import TA_LEFT, TA_CENTER

OUTPUT = Path(__file__).parent / "CHEAT_SHEET.pdf"

# ── Colour palette ──────────────────────────────────────────────────────────
NAVY    = colors.HexColor("#0f3460")
BLUE    = colors.HexColor("#3a7bd5")
TEAL    = colors.HexColor("#16213e")
MINT    = colors.HexColor("#7ecfb3")
GOLD    = colors.HexColor("#f6c90e")
BG_CARD = colors.HexColor("#f0f4ff")
BG_CODE = colors.HexColor("#1a1a2e")
BG_SEC  = colors.HexColor("#fafbff")
BORDER  = colors.HexColor("#dde3f5")
TEXT    = colors.HexColor("#1a1a2e")
MUTED   = colors.HexColor("#555555")
WHITE   = colors.white
GREEN   = colors.HexColor("#a8d8a8")

# ── Styles ──────────────────────────────────────────────────────────────────
def S(name, **kw):
    defaults = dict(fontName="Helvetica", fontSize=8, leading=11,
                    textColor=TEXT, spaceAfter=0, spaceBefore=0)
    defaults.update(kw)
    return ParagraphStyle(name, **defaults)

s_body   = S("body")
s_hint   = S("hint",  fontSize=7.5, textColor=MUTED, leading=10)
s_code   = S("code",  fontName="Courier", fontSize=7.5, leading=11,
             backColor=BG_CODE, textColor=colors.HexColor("#e2e8f0"),
             leftIndent=4, rightIndent=4, spaceBefore=2, spaceAfter=2,
             borderPad=4)
s_hdr    = S("hdr",   fontName="Helvetica-Bold", fontSize=9, textColor=WHITE)
s_sec    = S("sec",   fontName="Helvetica-Bold", fontSize=9, textColor=NAVY,
             spaceBefore=4, spaceAfter=3)
s_cover  = S("cover", fontName="Helvetica-Bold", fontSize=20, textColor=WHITE,
             alignment=TA_CENTER)
s_csub   = S("csub",  fontSize=9, textColor=colors.HexColor("#bbccee"),
             alignment=TA_CENTER)
s_th     = S("th",    fontName="Helvetica-Bold", fontSize=7.5, textColor=WHITE,
             alignment=TA_LEFT)
s_td     = S("td",    fontSize=7.5, leading=10)
s_tdcode = S("tdcode",fontName="Courier", fontSize=7, leading=10,
             textColor=NAVY)

def bold(text):
    return f"<b>{text}</b>"

def code_inline(text):
    return f'<font name="Courier" color="#0f3460">{text}</font>'

def section_title(num, title):
    """Numbered section header with circle badge."""
    return Paragraph(
        f'<font color="#3a7bd5">&#9679;</font> '
        f'<b><font color="#0f3460"> {num}  {title}</font></b>',
        S("stitle", fontName="Helvetica-Bold", fontSize=9, textColor=NAVY,
          spaceBefore=5, spaceAfter=3)
    )

def code_block(lines):
    """Monospace code block with dark background."""
    joined = "<br/>".join(
        line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for line in lines
    )
    return Paragraph(joined, s_code)

def simple_table(headers, rows, col_widths, row_colors=True):
    data = [[Paragraph(h, s_th) for h in headers]]
    for row in rows:
        data.append([Paragraph(str(c), s_td) for c in row])

    style = [
        ("BACKGROUND",  (0, 0), (-1, 0),  NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [BG_CARD, WHITE] if row_colors else [WHITE]),
        ("GRID",        (0, 0), (-1, -1),  0.3, BORDER),
        ("TOPPADDING",  (0, 0), (-1, -1),  3),
        ("BOTTOMPADDING",(0,0), (-1, -1),  3),
        ("LEFTPADDING", (0, 0), (-1, -1),  5),
        ("RIGHTPADDING",(0, 0), (-1, -1),  5),
        ("VALIGN",      (0, 0), (-1, -1),  "TOP"),
    ]
    return Table(data, colWidths=col_widths, style=TableStyle(style),
                 hAlign="LEFT")

# ── Page layout (A4 landscape, two columns) ─────────────────────────────────
PAGE_W, PAGE_H = landscape(A4)
MARGIN = 12 * mm
COL_GAP = 6 * mm
COL_W = (PAGE_W - 2 * MARGIN - COL_GAP) / 2

def build_cover(canvas, doc):
    canvas.saveState()
    # Navy gradient band
    canvas.setFillColor(NAVY)
    canvas.rect(0, PAGE_H - 28*mm, PAGE_W, 28*mm, fill=1, stroke=0)
    # Title
    canvas.setFont("Helvetica-Bold", 20)
    canvas.setFillColor(WHITE)
    canvas.drawString(MARGIN, PAGE_H - 14*mm, "Kinaxis MCP Server")
    # Subtitle
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(colors.HexColor("#bbccee"))
    canvas.drawString(MARGIN, PAGE_H - 22*mm,
                      "Team Cheat Sheet  ·  Quick-Start Guide")
    # Tool pills
    pills = [("web_search", "OpenAI / Serper"),
             ("open_url",   "URL → Markdown"),
             ("doc_search", "BM25 over DOCX/PDF/CSV")]
    x = PAGE_W - MARGIN - 3 * 75*mm + 10
    y = PAGE_H - 17*mm
    for name, desc in pills:
        canvas.setFillColor(BLUE)
        canvas.roundRect(x, y - 8*mm, 72*mm, 12*mm, 3, fill=1, stroke=0)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(WHITE)
        canvas.drawString(x + 3*mm, y - 1*mm, name)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#ccdaff"))
        canvas.drawString(x + 3*mm, y - 5.5*mm, desc)
        x += 75*mm
    # Footer
    canvas.setFont("Helvetica", 6.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 6*mm,
                      "BristleCone  ·  Kinaxis MCP Server  ·  2026-03-31")
    canvas.restoreState()

# ── Content builders ─────────────────────────────────────────────────────────

def left_column():
    items = []

    # 1 Prerequisites
    items.append(section_title("1", "Prerequisites"))
    items.append(simple_table(
        ["Requirement", "Version", "Check"],
        [["Python",         "3.11+", code_inline("python --version")],
         ["pip",            "any",   code_inline("pip --version")],
         ["OpenAI API key", "—",     "platform.openai.com"]],
        [28*mm, 18*mm, 46*mm]
    ))

    items.append(Spacer(1, 4))

    # 2 One-Time Setup
    items.append(section_title("2", "One-Time Setup"))
    items.append(code_block([
        "cd c:\\BristleCone\\Kinaxis-agent-build",
        "",
        "python -m venv unified_mcp\\venv",
        "unified_mcp\\venv\\Scripts\\activate   # Windows",
        "# source unified_mcp/venv/bin/activate  # Mac/Linux",
        "",
        "pip install -r unified_mcp\\requirements.txt",
    ]))

    items.append(Spacer(1, 4))

    # 3 Configure Environment
    items.append(section_title("3", "Configure Environment"))
    items.append(Paragraph(
        f"Create {code_inline('unified_mcp\\.env')} then export before running:", s_hint))
    items.append(Spacer(1, 2))
    items.append(code_block([
        "# Required",
        "OPENAI_API_KEY=sk-...",
        "",
        "# Optional: Serper.dev instead",
        "WEB_SEARCH_BACKEND=serper",
        "SERPER_API_KEY=your-serper-key",
        "",
        "# Optional: OpenAI model tuning",
        "OPENAI_SEARCH_MODEL=gpt-4o-mini-search-preview",
        "OPENAI_SEARCH_CTX=medium   # low | medium | high",
    ]))
    items.append(Paragraph(
        f"Windows CMD: {code_inline('set OPENAI_API_KEY=sk-...')}  &nbsp;"
        f"PowerShell: {code_inline('$env:OPENAI_API_KEY=\"sk-...\"')}  &nbsp;"
        f"Mac/Linux: {code_inline('export OPENAI_API_KEY=sk-...')}",
        s_hint))

    items.append(Spacer(1, 4))

    # 4 Run & Test
    items.append(section_title("4", "Run & Test"))
    items.append(code_block([
        "# Full suite — spawns server, calls all 3 tools",
        "python unified_mcp\\test_kinaxis_mcp.py",
        "",
        "# Specific groups",
        "python unified_mcp\\test_kinaxis_mcp.py --only list",
        "python unified_mcp\\test_kinaxis_mcp.py --only list,doc",
        "python unified_mcp\\test_kinaxis_mcp.py --only web",
        "",
        "# Show full tool output",
        "python unified_mcp\\test_kinaxis_mcp.py --verbose",
    ]))
    # Expected output mini-box
    items.append(Spacer(1, 3))
    exp_data = [[Paragraph("Expected output (all keys set)", S("eh", fontName="Helvetica-Bold",
                  fontSize=7, textColor=WHITE))],
                [Paragraph(
        "[PASS] Tool present: web_search<br/>"
        "[PASS] Tool present: open_url<br/>"
        "[PASS] Tool present: doc_search<br/>"
        "[PASS] Basic query  -- 3.2s  1842 chars<br/>"
        "[PASS] Fetch example.com  -- 0.8s  612 chars<br/>"
        "[PASS] Supply chain query -- 0.4s  hits=5<br/>"
        "Summary: 10 passed  0 failed  of 10",
        S("eb", fontName="Courier", fontSize=7, leading=10, textColor=GREEN,
          backColor=BG_CODE))]]
    items.append(Table(exp_data,
        colWidths=[COL_W - 4],
        style=TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), BLUE),
            ("BACKGROUND",   (0,1), (-1,1), BG_CODE),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
            ("LEFTPADDING",  (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("BOX",          (0,0), (-1,-1), 0.5, BLUE),
        ]), hAlign="LEFT"))

    items.append(Spacer(1, 4))

    # 5 Add Your Own Documents
    items.append(section_title("5", "Add Your Own Documents"))
    items.append(Paragraph(
        f"Drop files into {code_inline('unified_mcp/reference_docs/')} "
        f"— indexed automatically at server startup.", s_hint))
    items.append(Spacer(1, 2))
    items.append(simple_table(
        ["Format", "How it's chunked"],
        [[code_inline(".docx"), "By Word heading structure (H1 / H2 / H3…)"],
         [code_inline(".pdf"),  "One chunk per page"],
         [code_inline(".csv"),  "One chunk per row"]],
        [20*mm, 72*mm]
    ))

    return items


def right_column():
    items = []

    # 6 Claude Desktop
    items.append(section_title("6", "Connect to Claude Desktop"))
    items.append(Paragraph(
        f"Edit {code_inline('%APPDATA%\\\\Claude\\\\claude_desktop_config.json')}",
        s_hint))
    items.append(Spacer(1, 2))
    items.append(code_block([
        '{',
        '  "mcpServers": {',
        '    "kinaxis": {',
        '      "command": "C:\\\\...\\\\unified_mcp\\\\venv',
        '                      \\\\Scripts\\\\python.exe",',
        '      "args": [',
        '        "C:\\\\...\\\\unified_mcp',
        '             \\\\kinaxis_mcp_server.py"',
        '      ],',
        '      "env": { "OPENAI_API_KEY": "sk-..." }',
        '    }',
        '  }',
        '}',
    ]))
    items.append(Paragraph(
        "Restart Claude Desktop after saving. "
        "The <b>kinaxis</b> server appears in the MCP tools panel.", s_hint))

    items.append(Spacer(1, 4))

    # 7 Claude Code CLI
    items.append(section_title("7", "Connect to Claude Code CLI"))
    items.append(code_block([
        "claude mcp add kinaxis \\",
        '  --command "python" \\',
        '  --args "unified_mcp\\kinaxis_mcp_server.py" \\',
        '  --env "OPENAI_API_KEY=sk-..."',
        "",
        "claude mcp list     # verify registration",
    ]))

    items.append(Spacer(1, 4))

    # 8 Web Search Backends
    items.append(section_title("8", "Web Search Backends"))
    items.append(simple_table(
        ["Backend", "Env vars needed", "Best for"],
        [["OpenAI  (default)",
          code_inline("OPENAI_API_KEY"),
          "Summarized results, citations"],
         ["Serper.dev",
          code_inline("WEB_SEARCH_BACKEND=serper") + "<br/>" + code_inline("SERPER_API_KEY"),
          "Raw Google results, more links"]],
        [28*mm, 48*mm, 38*mm]
    ))

    items.append(Spacer(1, 4))

    # 9 Troubleshooting
    items.append(section_title("9", "Troubleshooting"))
    items.append(simple_table(
        ["Symptom", "Fix"],
        [
            [code_inline("ModuleNotFoundError: mcp"),
             code_inline("pip install -r requirements.txt")],
            [code_inline("OPENAI_API_KEY is not set"),
             "Export the var or add to Claude Desktop env config"],
            [code_inline("ModuleNotFoundError: openai"),
             code_inline("pip install openai>=1.60")],
            ["Doc index not available",
             f"Check {code_inline('reference_docs/')} has .docx/.pdf/.csv files"],
            ["Server crashes on startup",
             f"Run {code_inline('python kinaxis_mcp_server.py')} directly and read stderr"],
            ["No doc search results",
             "Try broader query terms; verify corpus has relevant content"],
        ],
        [52*mm, 60*mm]
    ))

    items.append(Spacer(1, 4))

    # 10 Folder Structure
    items.append(section_title("10", "Folder Structure"))
    items.append(code_block([
        "unified_mcp/",
        "├── kinaxis_mcp_server.py   <- run this",
        "├── test_kinaxis_mcp.py     <- test client",
        "├── doc_retrieval_index.py  <- BM25 engine",
        "├── serper_client.py        <- Serper backend",
        "├── reference_docs/         <- drop docs here",
        "│   ├── Kinaxis RFP.docx",
        "│   ├── Kraton Requirements.csv",
        "│   └── Pine Chemicals.pdf",
        "├── requirements.txt",
        "└── CHEAT_SHEET.pdf",
    ]))

    return items


# ── Build PDF ─────────────────────────────────────────────────────────────────

def build():
    CONTENT_TOP = PAGE_H - 30*mm   # below the cover band
    CONTENT_H   = CONTENT_TOP - MARGIN - 8*mm

    left_frame = Frame(
        MARGIN, MARGIN + 6*mm,
        COL_W, CONTENT_H,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="left"
    )
    right_frame = Frame(
        MARGIN + COL_W + COL_GAP, MARGIN + 6*mm,
        COL_W, CONTENT_H,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="right"
    )

    doc = BaseDocTemplate(
        str(OUTPUT),
        pagesize=landscape(A4),
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=30*mm, bottomMargin=MARGIN + 6*mm,
    )
    template = PageTemplate(
        id="main",
        frames=[left_frame, right_frame],
        onPage=build_cover,
    )
    doc.addPageTemplates([template])

    story = left_column() + right_column()
    doc.build(story)
    print(f"Done: {OUTPUT}  ({OUTPUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    print(f"Generating PDF -> {OUTPUT}")
    build()
