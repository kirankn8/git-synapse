"""Shared helpers for ingest-layer audit scripts."""
import os, subprocess, tempfile, shutil, pathlib

ENV = dict(os.environ)
ENV.update({
    "GIT_AUTHOR_NAME": "A U Thor", "GIT_AUTHOR_EMAIL": "a@example.com",
    "GIT_COMMITTER_NAME": "C O Mitter", "GIT_COMMITTER_EMAIL": "c@example.com",
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
})

def sh(cmd, cwd, env=None, check=True):
    e = dict(ENV); e.update(env or {})
    p = subprocess.run(cmd, cwd=str(cwd), env=e, capture_output=True, text=True, errors="replace")
    if check and p.returncode != 0:
        raise SystemExit(f"cmd failed {cmd}\n{p.stdout}\n{p.stderr}")
    return p

def newrepo(name="r"):
    d = pathlib.Path(tempfile.mkdtemp(prefix="gsaudit-"+name+"-"))
    sh(["git", "init", "-q", "-b", "main", "."], d)
    sh(["git", "config", "user.name", "A U Thor"], d)
    sh(["git", "config", "user.email", "a@example.com"], d)
    sh(["git", "config", "commit.gpgsign", "false"], d)
    return d

def commit(d, msg, allow_empty=False, env=None):
    args = ["git", "commit", "-q", "-m", msg]
    if allow_empty: args.append("--allow-empty")
    sh(args, d, env=env)
    return sh(["git", "rev-parse", "HEAD"], d).stdout.strip()

def write(d, path, content):
    p = pathlib.Path(d) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content)

def addall(d):
    sh(["git", "add", "-A", "."], d)

def bare_mirror(src):
    """Make a bare mirror of src (a working repo) the way gitops would."""
    m = pathlib.Path(tempfile.mkdtemp(prefix="gsmirror-")) / "m.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(src), str(m)], check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"],
                   cwd=str(m), check=True)
    subprocess.run(["git", "config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*"],
                   cwd=str(m), check=True)
    return m

def show(pc):
    return dict(sha=pc.sha[:8], subj=pc.subject,
                files=[(f.change_type, f.path, f.old_path, f.insertions, f.deletions,
                        f.is_binary, f.similarity) for f in pc.files])

def git_truth(mirror, sha):
    """Ground truth: what git itself says the commit touched (name-status)."""
    p = subprocess.run(["git", "show", "--no-color", "-z", "--name-status",
                        "--format=", "-M50%", sha], cwd=str(mirror),
                       capture_output=True, check=True)
    recs = p.stdout.split(b"\x00")
    out = []
    i = 0
    while i < len(recs):
        r = recs[i]
        if not r:
            i += 1; continue
        st = r.decode("utf-8", "replace")
        if st[:1] in ("R", "C"):
            out.append((st, recs[i+1].decode("utf-8","replace"), recs[i+2].decode("utf-8","replace")))
            i += 3
        else:
            out.append((st, recs[i+1].decode("utf-8","replace"), None))
            i += 2
    return out
