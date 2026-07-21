from collections import Counter
from pathlib import Path
import json
from app.domain import parse_problem

ROOT = Path(r"d:\codeing\07-share\0-plgl\102")
samples = json.loads((ROOT / "frontend-next" / "public" / "samples.json").read_text(encoding="utf-8"))["samples"]
sample = next(s for s in samples if s["id"] == "194ca29c-1ba1-4721-98c6-2af6382a64bb")
g = parse_problem(sample["problem"]).groups[0]
need = Counter(x.length for x in g.pipes)
stocks = sorted({s.length for s in g.stocks}, reverse=True)
Smax = max(stocks)
print("Smax", Smax, "stocks", stocks)
print("min_cut", g.min_cut_length, "min_weld", g.min_weld_distance, "blade", g.blade_margin)
print("max_joints sample", Counter(p.max_joints for p in g.pipes))

legacy_weld = [
    (4323,12000,3519,1411),
    (1246,12000,7672),
    (8584,12000),
    (3584,8000,9000),
    (9500,10749),
]
for wp in legacy_weld:
    sm = sum(wp)
    print("sum", wp, "=", sm, "demand", need.get(sm,0))

# remainder after peeling one Smax
long = sorted(L for L in need if L > Smax)
print("long unique", len(long), "demand", sum(need[L] for L in long))
rems = Counter()
for L in long:
    rem = L - Smax
    if rem > 0:
        rems[rem] += need[L]
print("top remainders after -Smax:", rems.most_common(15))
# check if remainder can be 2-split into complements of Smax
from app.solver import _legal_pattern
# find if any pipe has rem that bipartitions interestingly
hits = []
for L in long:
    rem = L - Smax
    if rem <= 0 or rem > Smax:
        # try 2 stocks
        rem2 = L - 2*Smax
        if rem2 > 0:
            hits.append(("2S", L, rem2, need[L]))
        continue
    # rem itself, or rem = a+b
    for a in range(g.min_cut_length, rem // 2 + 1):
        b = rem - a
        if b < g.min_cut_length or b > Smax or a > Smax:
            continue
        if a + b == rem and (a + b == rem):
            # check if a packs with something to Smax
            if (Smax - a) >= g.min_cut_length and (Smax - b) >= g.min_cut_length:
                if a in (1246,1411,3519,4323,7672) or b in (1246,1411,3519,4323,7672):
                    hits.append(("rem2", L, a, b, need[L]))
                    break
print("legacy-ish rem splits found", len(hits), "examples", hits[:10])

# How many pipes need 3+ segments (L > 2*Smax or rem > Smax)?
need3 = [L for L in long if L - Smax > Smax]  # rem > Smax means need at least 3 pieces if one is Smax? Actually if rem > Smax need another full or split
print("L such that L-Smax > Smax (need >=3 segs if embed 1 stock)", len(need3), "ex", need3[:5])
print("L with Smax < L <= 2S", sum(1 for L in long if L <= 2*Smax), "demand", sum(need[L] for L in long if L <= 2*Smax))
print("L with 2S < L <= 3S", sum(1 for L in long if 2*Smax < L <= 3*Smax), "demand", sum(need[L] for L in long if 2*Smax < L <= 3*Smax))
