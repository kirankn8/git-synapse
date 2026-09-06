import hashlib, psycopg, os
dsn = f"host={os.environ['POSTGRES_HOST']} user={os.environ['POSTGRES_USER']} password={os.environ['POSTGRES_PASSWORD']} dbname={os.environ['POSTGRES_DB']}"
def fp(cur):
    cur.execute("SELECT id, path, dir_path FROM file")
    rows = cur.fetchall()
    return len(rows), hashlib.md5(",".join(str(r[0]) for r in rows).encode()).hexdigest(), [r[0] for r in rows[:4]]
with psycopg.connect(dsn) as c:
    cur = c.cursor()
    for s in ("SET max_parallel_workers_per_gather=4","SET parallel_setup_cost=0",
              "SET parallel_tuple_cost=0","SET min_parallel_table_scan_size=0"):
        cur.execute(s)
    cur.execute("EXPLAIN SELECT id, path, dir_path FROM file")
    print([r[0] for r in cur.fetchall()])
    for i in range(5):
        print(i, fp(cur))
