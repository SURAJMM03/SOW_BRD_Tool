import json, sys
from pathlib import Path
sys.path.insert(0, "app")

records = json.loads(Path("app/image_chunks.json").read_text())
meta = {r["image_id"]: r for r in records}

# What are the images the LLM actually used?
print("=== IMAGES LLM CHOSE ===")
for iid in ["906c27756c60", "863ec0cc1bf1"]:
    r = meta.get(iid, {})
    print(f"  {iid}: {r.get('caption')} | {r.get('source_doc')} | keywords: {r.get('keywords', [])[:5]}")

print()

# What does search return for section 3.1 keywords?
from app.image_index import search_images
query = "map location plant production facility geography site distribution network"
print(f"=== SEARCH TOP 5 for section 3.1 keywords ===")
for r in search_images(query, top_k=5):
    print(f"  [{r['image_id']}] score={r['score']:.2f}  {r['caption']}  ({r['source_doc']})")

print()

# Simulate the full injection query (section kw + user kw)
# What were the user's doc_keywords for 3.1?  Let's check the keyword store
import os, glob
kw_files = glob.glob("app/screen3_keyword_*.json")
print(f"=== KEYWORD STORE FILES: {kw_files} ===")
for f in kw_files:
    data = json.loads(Path(f).read_text())
    if "3.1" in data:
        print(f"  {f} -> 3.1 keywords:")
        print(f"    doc_keywords: {data['3.1'].get('doc_keywords', [])}")
        print(f"    web_keywords: {data['3.1'].get('web_keywords', [])}")
