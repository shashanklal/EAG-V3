"""Quick diagnostic: verify embeddings + FAISS after web_search."""
import json
from pathlib import Path
import numpy as np
import faiss

state = Path(__file__).parent / "state"
items = json.loads((state / "memory.json").read_text())
ids_in_faiss = json.loads((state / "index_ids.json").read_text())

web_items = [i for i in items if i.get("value", {}).get("tool") == "web_search"]
print(f"web_search tool_outcomes in memory: {len(web_items)}")
print(f"  All have embeddings: {all(bool(i.get('embedding')) for i in web_items)}")
print(f"  All in FAISS: {all(i['id'] in ids_in_faiss for i in web_items)}")

# Show a sample
if web_items:
    sample = web_items[0]
    print(f"\nSample web_search item:")
    print(f"  id: {sample['id']}")
    print(f"  kind: {sample['kind']}")
    print(f"  descriptor: {sample['descriptor'][:120]}")
    print(f"  embedding dim: {len(sample['embedding'])}")
    print(f"  in FAISS: {sample['id'] in ids_in_faiss}")

# Verify FAISS index
idx = faiss.read_index(str(state / "index.faiss"))
print(f"\nFAISS index stats:")
print(f"  ntotal: {idx.ntotal}")
print(f"  dimension: {idx.d}")
print(f"  ids list len: {len(ids_in_faiss)}")
print(f"  Consistent (ntotal == len(ids)): {idx.ntotal == len(ids_in_faiss)}")

# Test query using first web_search embedding
if web_items:
    test_vec = np.array(web_items[0]["embedding"], dtype=np.float32)
    norm = float(np.linalg.norm(test_vec))
    if norm > 0:
        test_vec = test_vec / norm
    scores, idxs_arr = idx.search(test_vec.reshape(1, -1), 3)
    print(f"\nTest search (query = first web_search embedding):")
    for s, i in zip(scores[0].tolist(), idxs_arr[0].tolist()):
        if i >= 0:
            matched_id = ids_in_faiss[i]
            matched_item = next((x for x in items if x["id"] == matched_id), None)
            desc = matched_item["descriptor"][:80] if matched_item else "?"
            print(f"  score={s:.4f}, id={matched_id}, desc={desc}")
