"""Round 3: global gitconfig leak, big commit, submodule, failure handling."""
import sys, os, subprocess, pathlib, tempfile
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from git_synapse.ingest.parser import iter_commits
from git_synapse.ingest.gitops import _base_env, GitError

print("=========== A. does _base_env really ignore the user's global config? ===========")
env = _base_env()
print("  GIT_CONFIG_NOSYSTEM:", env.get("GIT_CONFIG_NOSYSTEM"))
print("  GIT_CONFIG_GLOBAL  :", env.get("GIT_CONFIG_GLOBAL"))
print("  HOME               :", env.get("HOME"))
home = pathlib.Path(env["HOME"])
gc = home / ".gitconfig"
d = newrepo("cfg")
write(d, "a.txt", "1\n"); addall(d); commit(d, "c1")
write(d, "b.txt", "2\n"); addall(d); commit(d, "c2")
m = bare_mirror(d)
print("  baseline:", [(c.subject, [f.path for f in c.files]) for c in iter_commits(m, rev="HEAD")])
# now poison the *global* config the way a real user's ~/.gitconfig could be
gc.write_text("[log]\n\tshowSignature = true\n[diff]\n\trenames = false\n[core]\n\tabbrev = 8\n")
try:
    got = [(c.subject, [f.path for f in c.files]) for c in iter_commits(m, rev="HEAD")]
    print("  with ~/.gitconfig [log] showSignature/[diff] renames=false:", got)
finally:
    gc.unlink()

print("\n=========== B. global config that breaks rename detection ===========")
d2 = newrepo("cfg2")
write(d2, "x.txt", "hello\nworld\nfoo\nbar\n"); addall(d2); commit(d2, "c1")
sh(["git","mv","x.txt","y.txt"], d2); commit(d2, "rename")
m2 = bare_mirror(d2)
print("  clean :", [(c.subject, [(f.change_type,f.path,f.old_path) for f in c.files]) for c in iter_commits(m2, rev="HEAD")])
gc.write_text("[diff]\n\tnoprefix = true\n[log]\n\tdate = relative\n[core]\n\tquotePath = true\n")
try:
    print("  poisoned:", [(c.subject, [(f.change_type,f.path,f.old_path) for f in c.files]) for c in iter_commits(m2, rev="HEAD")])
finally:
    gc.unlink()

print("\n=========== C. 10,000-file commit ===========")
d3 = newrepo("big")
import os as _os
for i in range(10000):
    write(d3, f"d{i//100}/f{i}.txt", f"line {i}\n")
addall(d3); commit(d3, "ten thousand")
m3 = bare_mirror(d3)
cs = list(iter_commits(m3, rev="HEAD"))
print("  commits:", len(cs), "files in commit:", len(cs[0].files),
      "unique:", len({f.path for f in cs[0].files}),
      "insertions:", cs[0].insertions)

print("\n=========== D. submodule ===========")
sub = newrepo("sub")
write(sub, "s.txt", "s1\n"); addall(sub); commit(sub, "sub c1")
d4 = newrepo("super")
write(d4, "a.txt", "1\n"); addall(d4); commit(d4, "c1")
sh(["git","-c","protocol.file.allow=always","submodule","add","-q",str(sub),"mod"], d4)
commit(d4, "add submodule")
write(sub, "s.txt", "s2\n"); addall(sub); commit(sub, "sub c2")
sh(["git","-c","protocol.file.allow=always","-C","mod","pull","-q","origin","main"], d4, check=False)
sh(["git","-C","mod","fetch","-q","origin"], d4, check=False)
sh(["git","-C","mod","reset","-q","--hard","origin/main"], d4, check=False)
addall(d4); commit(d4, "bump submodule")
m4 = bare_mirror(d4)
for c in iter_commits(m4, rev="HEAD"):
    print("   ", c.subject, [(f.change_type, f.path, f.insertions, f.deletions, f.is_binary) for f in c.files])

print("\n=========== E. git failure surfaced? ===========")
# 1. bad revision
try:
    list(iter_commits(m3, rev="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"))
    print("  bad rev: NO ERROR RAISED (returned empty)")
except GitError as e:
    print("  bad rev: GitError ->", str(e)[:120])
# 2. since_sha that does not exist
try:
    r = list(iter_commits(m3, rev="HEAD", since_shas=["0"*40]))
    print("  bad since_sha: NO ERROR, %d commits" % len(r))
except GitError as e:
    print("  bad since_sha: GitError ->", str(e)[:120])
# 3. mirror path that is not a repo
try:
    r = list(iter_commits(pathlib.Path("/tmp"), rev="HEAD"))
    print("  not-a-repo: NO ERROR, %d commits" % len(r))
except Exception as e:
    print("  not-a-repo:", type(e).__name__, str(e)[:160])
# 4. corrupt / missing objects mid-stream: truncate the pack
import shutil
m5 = pathlib.Path(tempfile.mkdtemp())/"c.git"
subprocess.run(["git","clone","--bare","-q",str(d3),str(m5)],check=True)
subprocess.run(["git","-C",str(m5),"unpack-objects"],check=False,capture_output=True)
objs = list((m5/"objects").rglob("*"))
print("  (corruption test) mirror objects:", len(objs))
