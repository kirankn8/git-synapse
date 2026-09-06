import numpy as np, math
from git_synapse.stats.contingency import Contingency
from git_synapse.stats import measures as M
np.seterr(all="ignore")
def T(a,b,c,d): return Contingency.from_counts([a],[a+b],[a+c],[a+b+c+d])
def v(f,a,b,c,d): return float(f(T(a,b,c,d))[0])

print("### 1. t_score at a==0 : guard returns 0 (the neutral value)")
for tbl in [(0,10,10,80),(0,50,50,900),(0,1,1,98),(5,45,45,5),(1,19,19,61),(2,18,18,62)]:
    a,b,c,d=tbl; na,nb,N=a+b,a+c,a+b+c+d; E=na*nb/N
    print(f"   a={a:3d} b={b:3d} c={c:3d} d={d:3d}  E={E:8.3f}  t_score={v(M.t_score,*tbl):9.4f}  (a-E)={a-E:9.3f}")
print("   => a=0,b=50,c=50,d=900 (E=5, maximally under-associated) scores 0.0,")
print("      i.e. HIGHER than a=5,b=45,c=45,d=5 and equal to exact independence.")

print("\n### 2. fager returns a non-zero score for a table with a zero marginal")
for tbl in [(0,0,10,90),(0,10,0,90),(0,0,1,99),(0,0,0,100)]:
    a,b,c,d=tbl
    print(f"   a={a} b={b} c={c} d={d}: n_a={a+b} n_b={a+c} -> fager={v(M.fager,*tbl):.6f}"
          f"  ochiai={v(M.ochiai,*tbl):.6f}  jaccard={v(M.jaccard,*tbl):.6f}")
print("   => fager alone emits a negative score where every sibling emits 0.")

print("\n### 3. fager true range")
best=1e9; arg=None
for na in range(1,60):
  for nb in range(1,60):
    x = 0.0 - 1/(2*math.sqrt(max(na,nb)))
    if x<best: best,arg=x,(na,nb)
print(f"   minimum attainable = {best} at n_a,n_b = {arg}  (registry declares lower=None)")

print("\n### 4. -0.0 leaking out of the significance measures")
for tbl in [(0,10,10,80),(1,0,0,0),(5,5,5,0)]:
    p=v(M.poisson_significance,*tbl); h=v(M.hypergeometric_significance,*tbl)
    print(f"   {tbl}: poisson={p!r} signbit={np.signbit(p)}   hyper={h!r} signbit={np.signbit(h)}")

print("\n### 5. npmi==+1 while every other measure says 'independent' at a==N")
for tbl in [(1,0,0,0),(10,0,0,0),(500,0,0,0)]:
    print(f"   {tbl}: npmi={v(M.npmi,*tbl):.3f}  pmi={v(M.pmi,*tbl):.3f}  phi={v(M.phi,*tbl):.3f} "
          f" chi2={v(M.chi_square,*tbl):.3f}  MI={v(M.mutual_information,*tbl):.3f} "
          f" lift={v(M.association_strength,*tbl):.3f}")

print("\n### 6. pmi ranking inversion: 'never together' outranks 'rarely together'")
for tbl in [(0,20,20,60),(1,19,19,61),(2,18,18,62),(4,16,16,64)]:
    print(f"   {tbl}: pmi={v(M.pmi,*tbl):8.4f}  npmi={v(M.npmi,*tbl):8.4f}  ppmi={v(M.ppmi,*tbl):8.4f}")

print("\n### 7. hamann 'neutral' claim")
for tbl in [(4,16,16,64),(25,25,25,25),(1,9,9,81)]:
    print(f"   independence {tbl}: hamann={v(M.hamann,*tbl):.4f}  (registry neutral=0.0)")
print("   hamann = 2*(a+d)/N - 1 ; it is 0 iff a+d == b+c, which has nothing to do with independence.")

print("\n### 8. from_counts silently clamps infeasible input (no error, no flag)")
for (ab,na,nb,N) in [(50,10,10,100),(5,10,10,4),(5,200,10,100),(-3,10,10,100),(0,60,60,100)]:
    t=Contingency.from_counts([ab],[na],[nb],[N])
    print(f"   in n_ab={ab},n_a={na},n_b={nb},N={N}  ->  a={t.a[0]},b={t.b[0]},c={t.c[0]},d={t.d[0]}")

print("\n### 9. numerical stability at repo scale")
for (ab,na,nb,N) in [(1,2,2,10**6),(10**5,2*10**5,2*10**5,10**6),(1,10**5,10**5,10**6)]:
    t=Contingency.from_counts([ab],[na],[nb],[N])
    print(f"   n_ab={ab} n_a={na} n_b={nb} N={N}: chi2={float(M.chi_square(t)[0]):.6g} "
          f"phi={float(M.phi(t)[0]):.6g} G2={float(M.log_likelihood_ratio(t)[0]):.6g} "
          f"pmi={float(M.pmi(t)[0]):.6g} poisson={float(M.poisson_significance(t)[0]):.6g} "
          f"hyper={float(M.hypergeometric_significance(t)[0]):.6g}")

print("\n### 10. is_degenerate() -- defined but is it used anywhere?")
import subprocess
print(subprocess.run(["grep","-rn","is_degenerate","/work/src","/work/web","/work/tests"],
                     capture_output=True,text=True).stdout or "   NOT REFERENCED ANYWHERE")

print("\n### 11. mining.py drift SQL NPMI vs Python npmi (same algebra, different degenerate rules)")
def sql_npmi(j,n,ma,mb):
    if j>0 and n>0 and ma>0 and mb>0 and j<n:
        return (math.log(j*n/(ma*mb))/math.log(2))/(-math.log(j/n)/math.log(2))
    return None
for (j,n,ma,mb) in [(5,100,10,10),(3,50,20,7),(4,4,4,4),(2,2,2,2),(10,10,10,10)]:
    a=j; na,nb,N=ma,mb,n; b,c,d=na-a,nb-a,N-na-nb+a
    py=v(M.npmi,a,b,c,d); s=sql_npmi(j,n,ma,mb)
    print(f"   j={j} N={n} m_a={ma} m_b={mb}: SQL={s if s is not None else 'NULL -> COALESCE 0'}   python npmi={py:.6f}")
