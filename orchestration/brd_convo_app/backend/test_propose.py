# test_propose.py
import os

# Enable mock Claude provider for local tests when no API key is available.
os.environ.setdefault("CLAUDE_MOCK", "1")

from app.conversation import propose_answer_from_research

q = "What is the primary data integration challenge for a supply chain blueprint?"
ctx = "## Web Insights\n- Example web result: companies often have disparate source systems.\n\n## Internal Docs\n- SampleDoc: contains interfaces to JD Edwards\n"
company = "Acme Corp"

print("Running propose_answer_from_research...")
out = propose_answer_from_research(q, ctx, company)
print("=== OUTPUT ===")
print(out)