from git_synapse.db.engine import query
print("synchronize_seqscans =", query("show synchronize_seqscans")[0])
print("max_parallel_workers_per_gather =", query("show max_parallel_workers_per_gather")[0])
r=query("EXPLAIN (FORMAT TEXT) SELECT id, path, dir_path FROM file WHERE repo_id = 368")
for x in r: print(x)
r=query("EXPLAIN (FORMAT TEXT) SELECT id, path, dir_path FROM file")
for x in r: print(x)
# does physical order differ from id order today?
rows = query("SELECT id FROM file WHERE repo_id=368")
ids=[x['id'] for x in rows]
print("rows:",len(ids),"is id-sorted?", ids==sorted(ids))
print("first 10 as returned:", ids[:10])
