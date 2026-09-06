"""Round 1: parser vs. git ground truth on a torture repo."""
import sys, os, subprocess, pathlib
sys.path.insert(0, "/work/scratch")
from ing_lib import *
from git_synapse.ingest.parser import iter_commits

d = newrepo("torture")
FAILS = []
def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print("   got :", got)
        print("   want:", want)
        FAILS.append(name)

# c1 normal
write(d, "a.txt", "1\n2\n3\n"); write(d, "dir/b.txt", "x\n")
addall(d); commit(d, "c1 normal")
# c2 empty commit
commit(d, "c2 empty", allow_empty=True)
# c3 delete
sh(["git", "rm", "-q", "dir/b.txt"], d); commit(d, "c3 delete")
# c4 re-add same path with different content
write(d, "dir/b.txt", "totally different content here\n"*3); addall(d); commit(d, "c4 readd")
# c5 exact rename
sh(["git", "mv", "a.txt", "a2.txt"], d); commit(d, "c5 exact rename")
# c6 similarity rename (modify while moving)
write(d, "a2.txt", "1\n2\n3\n4\n"); addall(d); commit(d, "c6 touch")
sh(["git", "mv", "a2.txt", "a3.txt"], d); write(d, "a3.txt", "1\n2\n3\n4\n5\n6\n")
addall(d); commit(d, "c7 similarity rename")
# c8 copy (needs -C to detect; parser only passes -M so expect A)
write(d, "a3copy.txt", "1\n2\n3\n4\n5\n6\n"); addall(d); commit(d, "c8 copy-as-add")
# c9 mode change only
sh(["git", "update-index", "--chmod=+x", "a3.txt"], d); commit(d, "c9 mode change")
# c10 weird paths
weird = [
    "sp ace.txt", 'qu"ote.txt', "back\\slash.txt", "uniçøde中文.txt",
    "new\nline.txt", ":colon-start.txt", "tab\there.txt", "-dash.txt",
    "a"*180 + ".txt",
]
for w in weird:
    write(d, w, "content of " + repr(w) + "\n")
addall(d); commit(d, "c10 weird paths")
# c11 CRLF + binary + symlink
write(d, "crlf.txt", "l1\r\nl2\r\n")
write(d, "bin.dat", bytes(range(256))*8)
os.symlink("a3.txt", str(d / "link.txt"))
addall(d); commit(d, "c11 crlf bin symlink")
# c12 author with comma and angle bracket
commit(d, "c12 odd author", allow_empty=True,
       env={"GIT_AUTHOR_NAME": "Doe, John <jd>", "GIT_AUTHOR_EMAIL": "Odd@Example.COM"})
# c13 timezone offset + identical timestamps
commit(d, "c13 tz", allow_empty=True,
       env={"GIT_AUTHOR_DATE": "2021-05-05T12:00:00+05:30",
            "GIT_COMMITTER_DATE": "2021-05-05T12:00:00-08:00"})
# c14 multiline body with \x1f and \x01 in it
sh(["git", "commit", "-q", "--allow-empty", "-m", "c14 subject",
    "-m", "body line1\nbody \x1f sep\nbody \x01 sentinel\n"], d)
# c15 rename back
sh(["git", "mv", "a3.txt", "a4.txt"], d); commit(d, "c15 rename fwd")
sh(["git", "mv", "a4.txt", "a3.txt"], d); commit(d, "c16 rename back")

m = bare_mirror(d)
os.environ["INCLUDE_MERGES"] = "0"
commits = list(iter_commits(m, rev="HEAD", reverse=True))
by_subj = {c.subject: c for c in commits}
print("n commits parsed:", len(commits))
for c in commits:
    print("  ", c.subject, "|", [(f.change_type, repr(f.path), f.old_path, f.insertions, f.deletions, f.is_binary) for f in c.files])

print("\n--- ground truth comparison ---")
for c in commits:
    truth = git_truth(m, c.sha)
    tset = sorted((t[0][:1], t[2] if t[2] is not None else t[1]) for t in truth)
    pset = sorted((f.change_type, f.path) for f in c.files)
    if tset != pset:
        print("MISMATCH", c.subject)
        print("   git   :", tset)
        print("   parser:", pset)
        FAILS.append("truth:" + c.subject)

print("\n--- specifics ---")
c10 = by_subj["c10 weird paths"]
got = sorted(f.path for f in c10.files)
check("c10 weird path set", got, sorted(weird))

c12 = by_subj["c12 odd author"]
check("c12 author name", c12.author_name, "Doe, John <jd>")
check("c12 author email lowered", c12.author_email, "odd@example.com")

c13 = by_subj["c13 tz"]
check("c13 authored tz offset", str(c13.authored_at), "2021-05-05 12:00:00+05:30")
check("c13 committed tz offset", str(c13.committed_at), "2021-05-05 12:00:00-08:00")

c14 = by_subj.get("c14 subject")
print("c14 present:", c14 is not None, "body:", repr(c14.body) if c14 else None)

print("\nFAILS:", FAILS)
