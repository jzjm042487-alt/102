from pathlib import Path
import json
from collections import Counter
from app.domain import parse_problem
from app.global_candidates import build_weld_pool, build_cut_pool
from app.solver_global import _lp_feasible

ROOT = Path(r"d:\codeing\07-share\0-plgl\102")
samples = json.loads((ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8"))["samples"]
g = parse_problem(next(s for s in samples if s["id"] == "194ca29c-1ba1-4721-98c6-2af6382a64bb")["problem"]).groups[0]

welds, alph = build_weld_pool(g, tier=3)
cuts = build_cut_pool(g, alph, tier=3)
print("alph", len(alph), "cuts", len(cuts), "has key segs", {4323,7672,1246,10749} <= alph)

# cut pattern (4323,7672)
found_cut = False
for c in cuts:
    cnt = c["counts"]
    if cnt.get(4323)==1 and cnt.get(7672)==1 and sum(cnt.values())==2:
        found_cut = True
        print("found cut", c)
        break
print("cut 4323+7672", found_cut)
found_cut2 = any(c["counts"].get(1246)==1 and c["counts"].get(10749)==1 for c in cuts)
print("cut 1246+10749", found_cut2)

# weld patterns for long pipes
need = Counter(p.length for p in g.pipes)
for idx, pipe in enumerate(g.pipes):
    if pipe.length <= max(s.length for s in g.stocks):
        continue
    pats = welds[idx]
    interesting = [p for p in pats if 12000 in p or 7672 in p or 4323 in p]
    print("pipe", pipe.length, "npats", len(pats), "interesting", interesting[:8])

print("lp", _lp_feasible(g, welds, cuts, 30.0))
