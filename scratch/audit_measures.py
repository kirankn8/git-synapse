"""Independent re-implementation + differential audit of the 31 measures."""
import math, itertools, sys
import numpy as np
from scipy import stats as sp
from git_synapse.stats.contingency import Contingency
from git_synapse.stats import measures as M
from git_synapse.stats.registry import MEASURES, BY_KEY

np.seterr(all="ignore")

# ---------- independent scalar reference implementations (from literature) ----------
def R(a,b,c,d):
    na, nb, N = a+b, a+c, a+b+c+d
    r = {}
    def sd(x,y): return float("nan") if y==0 else x/y
    r["jaccard"]        = sd(a, a+b+c)
    r["dice"]           = sd(2*a, 2*a+b+c)
    r["sorensen"]       = r["dice"]
    r["ochiai"]         = sd(a, math.sqrt(na*nb)) if na*nb>0 else float("nan")
    r["simpson"]        = sd(a, min(na,nb))
    r["braun_blanquet"] = sd(a, max(na,nb))
    r["kulczynski"]     = 0.5*(sd(a,na)+sd(a,nb)) if na>0 and nb>0 else float("nan")
    r["fager"]          = (r["ochiai"] - 1/(2*math.sqrt(max(na,nb)))) if (na*nb>0 and max(na,nb)>0) else float("nan")
    r["russell_rao"]    = sd(a, N)
    r["sokal_michener"] = sd(a+d, N)
    r["rogers_tanimoto"]= sd(a+d, a+d+2*(b+c))
    r["hamann"]         = sd((a+d)-(b+c), N)
    r["faith"]          = sd(a+0.5*d, N)
    # information theoretic
    lift = sd(a*N, na*nb) if na*nb>0 and N>0 else float("nan")
    r["association_strength"] = lift
    r["pmi"] = math.log2(lift) if (lift==lift and lift>0) else float("nan")
    if N>0 and 0 < a < N and na>0 and nb>0:
        r["npmi"] = r["pmi"]/(-math.log2(a/N))
    elif N>0 and a==0 and na>0 and nb>0:
        r["npmi"] = -1.0
    elif N>0 and a==N:
        r["npmi"] = 1.0
    else:
        r["npmi"] = float("nan")
    r["ppmi"] = max(r["pmi"],0.0) if r["pmi"]==r["pmi"] else float("nan")
    if N>0 and na>0 and nb>0 and na<N and nb<N:
        mi=0.0
        for o,rw,cl in ((a,na,nb),(b,na,N-nb),(c,N-na,nb),(d,N-na,N-nb)):
            if o>0:
                mi += (o/N)*math.log2((o/N)/((rw/N)*(cl/N)))
        r["mutual_information"]=mi
        g=0.0
        for o,rw,cl in ((a,na,nb),(b,na,N-nb),(c,N-na,nb),(d,N-na,N-nb)):
            e=rw*cl/N
            if o>0: g += o*math.log(o/e)
        r["log_likelihood_ratio"]=2*g
        r["chi_square"]= N*(a*d-b*c)**2/(na*nb*(c+d)*(b+d))
        r["phi"]=(a*d-b*c)/math.sqrt(na*nb*(c+d)*(b+d))
        r["cramers_v"]=abs(r["phi"])
    else:
        for k in ("mutual_information","log_likelihood_ratio","chi_square","phi","cramers_v"):
            r[k]=float("nan")
    E = na*nb/N if N>0 else float("nan")
    r["t_score"] = (a-E)/math.sqrt(a) if a>0 and N>0 else float("nan")
    r["z_score"] = (a-E)/math.sqrt(E) if (E==E and E>0) else float("nan")
    ad, bc = a*d, b*c
    r["yules_q"] = (ad-bc)/(ad+bc) if ad+bc>0 else float("nan")
    sad, sbc = math.sqrt(ad), math.sqrt(bc)
    r["yules_y"] = (sad-sbc)/(sad+sbc) if sad+sbc>0 else float("nan")
    den = (a+d)**2+(b+c)**2
    r["michael"] = 4*(ad-bc)/den if den>0 else float("nan")
    r["confidence_ab"] = sd(a,na)
    r["confidence_ba"] = sd(a,nb)
    # significance as -log10 tail
    def nl(p):
        if not np.isfinite(p): return float("nan")
        p=min(max(p,1e-300),1.0); return min(max(-math.log10(p),0.0),300.0)
    r["poisson_significance"] = nl(sp.poisson.sf(a-1, max(E,1e-12))) if N>0 else float("nan")
    r["hypergeometric_significance"] = nl(sp.hypergeom.sf(a-1, N, na, nb)) if N>0 else float("nan")
    return r

def lib(a,b,c,d):
    t = Contingency.from_counts(n_ab=[a], n_a=[a+b], n_b=[a+c], n_total=[a+b+c+d])
    return {s.key: float(s.compute(t)[0]) for s in MEASURES}

KEYS=[s.key for s in MEASURES]

# ---------------- 1. random differential test ----------------
rng=np.random.default_rng(7)
bad={k:[] for k in KEYS}
for _ in range(8000):
    N=int(rng.integers(2,200))
    na=int(rng.integers(1,N)); nb=int(rng.integers(1,N))
    lo=max(0,na+nb-N); hi=min(na,nb)
    a=int(rng.integers(lo,hi+1))
    b,c,d=na-a,nb-a,N-na-nb+a
    ref=R(a,b,c,d); got=lib(a,b,c,d)
    for k in KEYS:
        rv=ref[k]
        if rv!=rv: continue          # reference undefined -> skip
        gv=got[k]
        tol=1e-7*max(1.0,abs(rv))
        if not (abs(gv-rv)<=tol or (abs(rv)>1e10 and abs(gv-rv)/abs(rv)<1e-9)):
            if len(bad[k])<3: bad[k].append((a,b,c,d,gv,rv))
print("=== RANDOM DIFFERENTIAL (8000 valid tables) ===")
for k in KEYS:
    if bad[k]: print(f"  MISMATCH {k}: {bad[k]}")
print("  (no line above a key => agrees with reference everywhere it is defined)")

# ---------------- 2. range check over random + degenerate ----------------
print("\n=== RANGE CHECK vs registry lower/upper ===")
tables=[]
for _ in range(20000):
    N=int(rng.integers(1,300)); na=int(rng.integers(0,N+1)); nb=int(rng.integers(0,N+1))
    lo=max(0,na+nb-N); hi=min(na,nb); a=int(rng.integers(lo,hi+1))
    tables.append((a,na-a,nb-a,N-na-nb+a))
for a,b,c,d in itertools.product(range(4),repeat=4): tables.append((a,b,c,d))
arr=np.array(tables)
t=Contingency.from_counts(n_ab=arr[:,0],n_a=arr[:,0]+arr[:,1],n_b=arr[:,0]+arr[:,2],n_total=arr.sum(1))
for s in MEASURES:
    v=s.compute(t)
    obs_lo,obs_hi=float(np.nanmin(v)),float(np.nanmax(v))
    msgs=[]
    if s.lower is not None and obs_lo < s.lower-1e-9:
        i=int(np.argmin(v)); msgs.append(f"below lower {s.lower}: {obs_lo:.6g} at {tuple(map(int,tables[i]))}")
    if s.upper is not None and obs_hi > s.upper+1e-9:
        i=int(np.argmax(v)); msgs.append(f"above upper {s.upper}: {obs_hi:.6g} at {tuple(map(int,tables[i]))}")
    if not np.all(np.isfinite(v)): msgs.append("NON-FINITE VALUES")
    flag=""
    if s.lower is None and obs_lo > -1e6: flag=f"  [declared unbounded below, observed min {obs_lo:.4g}]"
    if s.upper is None and obs_hi < 1e6: flag+=f"  [declared unbounded above, observed max {obs_hi:.4g}]"
    print(f"  {s.key:30s} obs[{obs_lo:12.6g},{obs_hi:12.6g}] declared[{s.lower},{s.upper}] {'; '.join(msgs)}{flag}")

# ---------------- 3. hand-chosen analytic cases ----------------
print("\n=== ANALYTIC / DEGENERATE CASES ===")
cases={
 "independence 25/25/25/25":(25,25,25,25),
 "independence 10/10/10/10":(10,10,10,10),
 "independence a=2,b=8,c=8,d=32":(2,8,8,32),
 "perfect assoc a=10,d=10":(10,0,0,10),
 "perfect anti  b=10,c=10":(0,10,10,0),
 "a=0 (never together), na=nb=10, N=100":(0,10,10,80),
 "b=0 (A subset of B)":(5,0,5,90),
 "c=0 (B subset of A)":(5,5,0,90),
 "d=0":(5,5,5,0),
 "n_a=0":(0,0,10,90),
 "n_b=0":(0,10,0,90),
 "n_a=0 and n_b=0":(0,0,0,100),
 "N=0":(0,0,0,0),
 "a=N (both in every commit)":(10,0,0,0),
 "single obs a=1 only":(1,0,0,0),
 "a=1,b=0,c=0,d=99":(1,0,0,99),
}
for name,(a,b,c,d) in cases.items():
    got=lib(a,b,c,d); ref=R(a,b,c,d)
    print(f"\n-- {name}  (a={a},b={b},c={c},d={d})")
    for k in KEYS:
        rv=ref[k]; gv=got[k]
        note=""
        if rv!=rv: note="  <- reference UNDEFINED, lib returns this"
        elif abs(gv-rv)>1e-7*max(1,abs(rv)): note=f"  <- DISAGREE, ref={rv:.6g}"
        print(f"     {k:30s} = {gv:14.6g}{note}")

# ---------------- 4. symmetry ----------------
print("\n=== SYMMETRY m(A,B) vs m(B,A) (swap b<->c) ===")
asym={k:None for k in KEYS}
for _ in range(4000):
    N=int(rng.integers(2,200)); na=int(rng.integers(0,N+1)); nb=int(rng.integers(0,N+1))
    lo=max(0,na+nb-N); hi=min(na,nb); a=int(rng.integers(lo,hi+1))
    b,c,d=na-a,nb-a,N-na-nb+a
    f=lib(a,b,c,d); g=lib(a,c,b,d)
    for k in KEYS:
        if asym[k] is None and abs(f[k]-g[k])>1e-9*max(1,abs(f[k])):
            asym[k]=(a,b,c,d,f[k],g[k])
for s in MEASURES:
    directional = s.family=="Directional (asymmetric)"
    is_asym = asym[s.key] is not None
    verdict = "OK" if is_asym==directional else "*** FAMILY/SYMMETRY MISMATCH ***"
    print(f"  {s.key:30s} asymmetric={is_asym!s:5s} family={s.family:26s} {verdict}"
          + (f" e.g. {asym[s.key]}" if is_asym and not directional else ""))

# ---------------- 5. neutral value at independence ----------------
print("\n=== NEUTRAL VALUE AT EXACT INDEPENDENCE ===")
ind=[(4,16,16,64),(1,9,9,81),(25,25,25,25),(6,4,24,16),(2,18,8,72)]
for s in MEASURES:
    if s.neutral is None: continue
    for (a,b,c,d) in ind:
        v=lib(a,b,c,d)[s.key]
        if abs(v-s.neutral)>1e-9:
            print(f"  {s.key}: at independence (a={a},b={b},c={c},d={d}) got {v:.6g}, neutral declared {s.neutral}")
print("  (done)")
