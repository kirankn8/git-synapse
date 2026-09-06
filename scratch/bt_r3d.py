"""Two concurrent scans of `file` under the DEFAULT (serial seq scan) plan."""
import hashlib, os, threading, psycopg
dsn = f"host={os.environ['POSTGRES_HOST']} user={os.environ['POSTGRES_USER']} password={os.environ['POSTGRES_PASSWORD']} dbname={os.environ['POSTGRES_DB']}"
res = {}
def scan(tag, delay=0.0):
    import time; time.sleep(delay)
    with psycopg.connect(dsn) as c:
        cur = c.cursor(); cur.execute("SELECT id, path, dir_path FROM file")
        rows = cur.fetchall()
        res[tag] = (len(rows), hashlib.md5(",".join(str(r[0]) for r in rows).encode()).hexdigest(), rows[0][0])
ts = [threading.Thread(target=scan, args=(i, i*0.35)) for i in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
for k in sorted(res): print(k, res[k])
print("distinct orderings:", len({v[1] for v in res.values()}))
