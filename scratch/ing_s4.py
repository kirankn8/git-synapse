import sys
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from git_synapse.ingest.parser import iter_commits
from git_synapse.ingest import gitops

for kind in ("lightweight", "annotated"):
    print("========== tag kind:", kind, "==========")
    d = newrepo("t-"+kind)
    write(d,"x.txt","x1\nx2\nx3\nx4\n"); addall(d); commit(d,"base")
    sh(["git","checkout","-q","-b","rel"], d)
    write(d,"relonly.txt","r\n"); addall(d); commit(d,"REL ONLY COMMIT")
    if kind == "annotated":
        sh(["git","tag","-a","v2.0","-m","rel"], d)
    else:
        sh(["git","tag","v2.0"], d)
    sh(["git","checkout","-q","main"], d)
    write(d,"x.txt","x1\nx2\nx3\nx4\nmain\n"); addall(d); commit(d,"main work")
    m = bare_mirror(d); sh(["git","fetch","-q","origin","+refs/tags/*:refs/tags/*"], m)
    print("  refs:", sh(["git","for-each-ref","--format=%(refname) %(objecttype) %(objectname)"], m).stdout.replace("\n"," | "))
    print("  git log HEAD --tags      :", sh(["git","log","--oneline","--no-merges","HEAD","--tags"], m).stdout.replace("\n"," | "))
    print("  git log --tags HEAD      :", sh(["git","log","--oneline","--no-merges","--tags","HEAD"], m).stdout.replace("\n"," | "))
    print("  rev-list --no-walk --tags HEAD:", sh(["git","rev-list","--no-walk","--tags","HEAD"], m).stdout.split())
    print("  rev-list --tags HEAD --no-walk:", sh(["git","rev-list","--tags","HEAD","--no-walk"], m).stdout.split())
    print("  iter_commits(include_tags=True):", [c.subject for c in iter_commits(m, rev="HEAD", include_tags=True)])
    print("  gitops.ref_tips():", gitops.ref_tips(m))
    print("  gitops.read_tags():", gitops.read_tags(m, "main"))
    print("  gitops.replayed_commits():", gitops.replayed_commits(m, "main"))
