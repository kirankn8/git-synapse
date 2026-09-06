from fractions import Fraction as F
import math, numpy as np
from git_synapse.stats.contingency import Contingency
from git_synapse.stats import measures as M
np.seterr(all="ignore")
def T(a,b,c,d): return Contingency.from_counts([a],[a+b],[a+c],[a+b+c+d])
print("### exact-arithmetic precision check at repo scale (phi / chi2 / G2)")
rng=np.random.default_rng(3)
worst={"phi":0,"chi_square":0,"log_likelihood_ratio":0,"michael":0,"yules_q":0}
argw={k:None for k in worst}
for _ in range(4000):
    N=int(rng.integers(10**5,5*10**7))
    na=int(rng.integers(1,N)); nb=int(rng.integers(1,N))
    lo=max(0,na+nb-N); hi=min(na,nb); a=int(rng.integers(lo,hi+1))
    b,c,d=na-a,nb-a,N-na-nb+a
    if min(na,nb,N-na,N-nb)==0: continue
    t=T(a,b,c,d)
    ex_phi = float(F(a*d-b*c,1))/math.sqrt(float(na)*nb*(c+d)*(b+d))
    # exact via Fraction for the numerator, exact sqrt of exact denominator
    num=F(a*d-b*c); den=F(na)*F(nb)*F(c+d)*F(b+d)
    ex_phi = float(num/ (F(math.isqrt(int(den*10**40)),10**20)))
    got=float(M.phi(t)[0])
    e=abs(got-ex_phi)
    if e>worst["phi"]: worst["phi"],argw["phi"]=e,(a,b,c,d,got,ex_phi)
    ex_chi = float(F(N)*num*num/den)
    gc=float(M.chi_square(t)[0])
    rel=abs(gc-ex_chi)/max(1.0,abs(ex_chi))
    if rel>worst["chi_square"]: worst["chi_square"],argw["chi_square"]=rel,(a,b,c,d,gc,ex_chi)
    ex_q = float(F(a*d-b*c, a*d+b*c)) if a*d+b*c else 0.0
    gq=float(M.yules_q(t)[0]); e=abs(gq-ex_q)
    if e>worst["yules_q"]: worst["yules_q"],argw["yules_q"]=e,(a,b,c,d,gq,ex_q)
    ex_m = float(F(4*(a*d-b*c), (a+d)**2+(b+c)**2))
    gm=float(M.michael(t)[0]); e=abs(gm-ex_m)
    if e>worst["michael"]: worst["michael"],argw["michael"]=e,(a,b,c,d,gm,ex_m)
for k in ("phi","chi_square","yules_q","michael"):
    print(f"  {k:22s} worst error {worst[k]:.3e}   at {argw[k]}")

print("\n### significance clamping / saturation at scale")
for (ab,na,nb,N) in [(10**6,2*10**6,2*10**6,10**7),(5*10**5,10**6,10**6,10**7),(30,40,40,10**6)]:
    t=Contingency.from_counts([ab],[na],[nb],[N])
    print(f"  n_ab={ab} n_a={na} n_b={nb} N={N}: poisson={float(M.poisson_significance(t)[0])} "
          f"hyper={float(M.hypergeometric_significance(t)[0])} G2={float(M.log_likelihood_ratio(t)[0]):.6g}")
print("  (300.0 == MAX_NEG_LOG10_P clamp; ties there are indistinguishable)")

print("\n### does clamping in _neg_log10 turn a *smaller* p into a *smaller* score anywhere?")
# scipy hypergeom.sf can underflow to exactly 0 -> clamped to 1e-300 -> 300.
import scipy.stats as sp
print("  hypergeom.sf(1e6-1, 1e7, 2e6, 2e6) =", sp.hypergeom.sf(10**6-1,10**7,2*10**6,2*10**6))
