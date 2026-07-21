from pathlib import Path
import json
from app.domain import parse_problem
from app.global_candidates import (
    build_seed_alphabet, _remainder_pack_parts, _pipe_stock_remainders,
    _dominant_remainders, _stock_lengths,
)

ROOT = Path(r"d:\codeing\07-share\0-plgl\102")
samples = json.loads((ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8"))["samples"]
sample = next(s for s in samples if s["id"] == "194ca29c-1ba1-4721-98c6-2af6382a64bb")
g = parse_problem(sample["problem"]).groups[0]
stocks = _stock_lengths(g)
max_stock = stocks[-1]
min_cut = max(g.min_cut_length, 1)
min_internal = max(g.min_weld_distance, min_cut)
rems = _pipe_stock_remainders(g, stocks, max_stock, min_cut, min_internal)
dom = _dominant_remainders(g, stocks, max_stock, min_cut)
forced = set(stocks) | rems
for pipe in g.pipes:
    if pipe.length <= max_stock:
        forced.add(pipe.length)
parts = _remainder_pack_parts(rems, stocks, forced, min_cut, max_stock, fill_targets=dom)
seed = build_seed_alphabet(g, seed_cap=160)
legacy = {1246, 1411, 3519, 3584, 4323, 4930, 7672, 8584, 10749, 12000, 8000, 9000, 9500}
print("rems", len(rems), "dom", sorted(dom), "parts", len(parts))
print("parts hit", sorted(legacy & parts), "miss", sorted(legacy - parts - forced))
print("seed size", len(seed), "hit", sorted(legacy & seed), "miss", sorted(legacy - seed))
