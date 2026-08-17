from pathlib import Path
import re

txt = Path("app/static/exports/BD_1_1_20260508_131058.txt").read_text(encoding="utf-8")

# Extract section 3.1 block
match = re.search(r"Section 3\.1.*?(?=Section 3\.2|={20}|\Z)", txt, re.DOTALL)
section = match.group(0) if match else "(section 3.1 not found)"

print("=== SECTION 3.1 CONTENT ===")
print(section)
print()

# Find any image-like references
print("=== ALL IMAGE REFERENCES IN FULL DOC ===")
patterns = [
    r'<IMAGE[^>]*/?>',
    r'!\[[^\]]*\]\([^\)]+\)',
    r'attachment:[a-f0-9]+',
    r'\[IMAGE[^\]]*\]',
    r'4d9329ccfdb5',   # the specific map image id
]
for pat in patterns:
    hits = re.findall(pat, txt, re.IGNORECASE)
    if hits:
        print(f"Pattern {pat!r}: {hits}")
    else:
        print(f"Pattern {pat!r}: (none)")
