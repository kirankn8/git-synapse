"""Round 2: colon-path phantom, leading-newline path, leading-space path, merges."""
import sys, os, subprocess
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from git_synapse.ingest.parser import iter_commits, split_path

print("=========== A. colon path is LAST in raw block ===========")
d = newrepo("colon")
write(d, "0first.txt", "x\n"); addall(d); commit(d, "base")
write(d, "1a.txt", "aaa\n")
write(d, ":zz.txt", "bbb\n")     # ':' (0x3a) sorts after digits -> last
addall(d); commit(d, "colon last")
m = bare_mirror(d)
for c in iter_commits(m, rev="HEAD"):
    print(" ", c.subject, "->", [(f.change_type, repr(f.path), f.insertions, f.deletions) for f in c.files])
print(" git truth:", git_truth(m, "HEAD"))

print("\n=========== B. path STARTS with newline ===========")
d2 = newrepo("nl")
write(d2, "z.txt", "x\n"); addall(d2); commit(d2, "base")
write(d2, "\nleading.txt", "y\n")   # filename begins with \n
addall(d2); commit(d2, "leading newline path")
m2 = bare_mirror(d2)
for c in iter_commits(m2, rev="HEAD"):
    print(" ", c.subject, "->", [(f.change_type, repr(f.path)) for f in c.files])
print(" git truth:", [(a, repr(b)) for a,b,_ in git_truth(m2, "HEAD")])

print("\n=========== C. leading/trailing space in path (split_path) ===========")
for p in [" lead.txt", "trail.txt ", "dir/ mid .txt", "\nnl.txt"]:
    print("  split_path(%r) = %r" % (p, split_path(p)))

print("\n=========== D. merges: 2-parent and octopus ===========")
d3 = newrepo("merge")
write(d3, "base.txt", "b\n"); addall(d3); commit(d3, "base")
for br in ("f1", "f2", "f3"):
    sh(["git","checkout","-q","-b",br,"main"], d3)
    write(d3, br+".txt", br+"\n"); addall(d3); commit(d3, "on "+br)
sh(["git","checkout","-q","main"], d3)
sh(["git","merge","-q","--no-ff","-m","two-parent merge","f1"], d3)
sh(["git","merge","-q","--no-ff","-m","octopus merge","f2","f3"], d3)
m3 = bare_mirror(d3)
print(" --- include_merges=False (default)")
for c in iter_commits(m3, rev="HEAD", include_merges=False):
    print("   ", c.subject, "parents=", len(c.parents), "files=", [f.path for f in c.files])
print(" --- include_merges=True")
for c in iter_commits(m3, rev="HEAD", include_merges=True):
    print("   ", c.subject, "parents=", len(c.parents), "is_merge=", c.is_merge,
          "files=", [f.path for f in c.files])

print("\n=========== E. empty repo ===========")
d4 = newrepo("empty")
m4 = bare_mirror(d4) if False else None
import tempfile, pathlib
m4 = pathlib.Path(tempfile.mkdtemp())/"e.git"
subprocess.run(["git","init","--bare","-q",str(m4)], check=True)
print("  commits:", list(iter_commits(m4, rev="HEAD")))

print("\n=========== F. --tags placement after rev ===========")
d5 = newrepo("tags")
write(d5, "a.txt","1\n"); addall(d5); commit(d5, "main1")
sh(["git","checkout","-q","-b","rel"], d5)
write(d5, "rel.txt","r\n"); addall(d5); commit(d5, "on release branch only")
sh(["git","tag","v1.0"], d5)
sh(["git","checkout","-q","main"], d5)
write(d5,"a.txt","2\n"); addall(d5); commit(d5,"main2")
m5 = bare_mirror(d5)
sh(["git","fetch","-q","origin","+refs/tags/*:refs/tags/*"], m5)
print("  refs in mirror:", sh(["git","for-each-ref","--format=%(refname)"], m5).stdout.split())
print("  include_tags=False:", [c.subject for c in iter_commits(m5, rev="HEAD", include_tags=False)])
print("  include_tags=True :", [c.subject for c in iter_commits(m5, rev="HEAD", include_tags=True)])
