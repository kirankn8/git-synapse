"""Shared helpers for the derivation-layer audit (scratch only)."""
from __future__ import annotations
import datetime as dt, os
import psycopg
from git_synapse.config import get_config

NOW = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

def conn(autocommit=False):
    return psycopg.connect(get_config().db.dsn, autocommit=autocommit)

def fresh_db(c, schema=True):
    c.execute("DROP SCHEMA public CASCADE")
    c.execute("CREATE SCHEMA public")
    if schema:
        from importlib import resources
        ddl = resources.files("git_synapse.db").joinpath("schema.sql").read_text(encoding="utf-8")
        c.execute(ddl)

def mk_repo(c, full_name, host="github.com"):
    owner, _, name = full_name.partition("/")
    return c.execute(
        "INSERT INTO repo (provider, host, owner, name, full_name) VALUES ('git',%s,%s,%s,%s) RETURNING id",
        (host, owner, name, full_name)).fetchone()[0]

def mk_author(c, email):
    cols = [r[0] for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='author'").fetchall()]
    return c.execute("INSERT INTO author (email, display_name) VALUES (%s,%s) RETURNING id",
                     (email, email)).fetchone()[0]

def mk_file(c, repo_id, path):
    d, _, base = path.rpartition("/")
    ext = ("." + base.rsplit(".",1)[1]) if "." in base else None
    depth = path.count("/")
    return c.execute(
        "INSERT INTO file (repo_id, path, dir_path, basename, extension, depth)"
        " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (repo_id, path, d, base, ext, depth)).fetchone()[0]

def mk_commit(c, repo_id, sha, files, when, author_id=None, is_merge=False,
              is_replay=False, file_ids=None):
    """files: list of paths (created lazily) or pass file_ids."""
    n = len(files if file_ids is None else file_ids)
    cid = c.execute(
        "INSERT INTO commit (repo_id, sha, author_id, authored_at, committed_at, subject,"
        " parent_count, is_merge, is_replay, n_files) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (repo_id, sha, author_id, when, when, sha, 2 if is_merge else 1, is_merge, is_replay, n)).fetchone()[0]
    ids = file_ids if file_ids is not None else files
    for fid in ids:
        c.execute("INSERT INTO commit_file (commit_id, file_id, repo_id, change_type)"
                  " VALUES (%s,%s,%s,'M')", (cid, fid, repo_id))
    return cid

def dump(c, sql, params=None, label=""):
    cur = c.execute(sql, params)
    rows = cur.fetchall()
    cols = [d.name for d in cur.description] if cur.description else []
    print(f"--- {label}")
    print("  " + " | ".join(cols))
    for r in rows:
        print("  " + " | ".join(str(x) for x in r))
    return rows

def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "**FAIL** ") + name + f": got={got!r} want={want!r}")
    return ok
