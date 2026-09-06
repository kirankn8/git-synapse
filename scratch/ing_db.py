"""DB harness for ingest audit against gs-testdb."""
import sys, os, uuid, json
sys.path.insert(0, "/work/scratch")
from git_synapse.db.engine import connection
from git_synapse.ingest.github import RepoRecord
from git_synapse.ingest.store import upsert_repo, load_commits, load_tags
from git_synapse.ingest.parser import iter_commits
from git_synapse.ingest import gitops

def fresh_repo(tag=None):
    name = "aud-" + (tag or uuid.uuid4().hex[:8])
    rec = RepoRecord(github_id=None, owner="audit", name=name,
                     full_name="audit/" + name, provider="git", host="audit.local",
                     default_branch="main")
    with connection() as c:
        rid = upsert_repo(rec, c)
        # wipe anything left over
        c.execute("DELETE FROM commit_file WHERE repo_id=%s", (rid,))
        c.execute("DELETE FROM commit_parent WHERE repo_id=%s", (rid,))
        c.execute("DELETE FROM commit WHERE repo_id=%s", (rid,))
        c.execute("DELETE FROM file_alias WHERE repo_id=%s", (rid,))
        c.execute("DELETE FROM file WHERE repo_id=%s", (rid,))
        c.execute("DELETE FROM ref_tag WHERE repo_id=%s", (rid,))
    return rid

def ingest(rid, mirror, watermarks=None, include_tags=True, mark_replays=True):
    wm = [s for s in (watermarks or []) if gitops.commit_exists(mirror, s)]
    commits = iter_commits(mirror, since_shas=wm, reverse=True, include_tags=include_tags)
    with connection() as conn:
        stats = load_commits(rid, commits, conn)
        branch = gitops.default_branch(mirror)
        replays = gitops.replayed_commits(mirror, branch) if mark_replays else set()
        n = 0
        if replays:
            n = conn.execute(
                "UPDATE commit SET is_replay=TRUE, pair_eligible=FALSE"
                " WHERE repo_id=%s AND sha=ANY(%s) AND NOT is_replay",
                (rid, list(replays))).rowcount or 0
        load_tags(rid, gitops.read_tags(mirror, branch), conn)
    return stats, gitops.ref_tips(mirror), n, replays

def q(sql, params=()):
    with connection() as c:
        return c.execute(sql, params).fetchall()

def dump(rid, label=""):
    print(f"  -- files ({label})")
    for r in q("SELECT id, path, dir_path, basename, is_deleted FROM file WHERE repo_id=%s ORDER BY id", (rid,)):
        print("     file", r)
    print(f"  -- aliases ({label})")
    for r in q("SELECT old_path, file_id FROM file_alias WHERE repo_id=%s ORDER BY old_path", (rid,)):
        print("     alias", r)
    print(f"  -- commits ({label})")
    for r in q("SELECT sha, subject, n_files, is_merge, pair_eligible, is_replay FROM commit WHERE repo_id=%s ORDER BY authored_at, sha", (rid,)):
        print("     commit", r[0][:8], r[1][:34], "n_files=%s merge=%s elig=%s replay=%s" % r[2:])
    print(f"  -- commit_file ({label})")
    for r in q("""SELECT c.subject, f.path, cf.change_type, cf.old_path, cf.file_id
                  FROM commit_file cf JOIN commit c ON c.id=cf.commit_id
                  JOIN file f ON f.id=cf.file_id WHERE cf.repo_id=%s
                  ORDER BY c.authored_at, c.sha, f.path""", (rid,)):
        print("     cf", r)
