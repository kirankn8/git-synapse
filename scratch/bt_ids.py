from git_synapse.db.engine import query
names = ('laravel/framework','google/pytype','google/flatbuffers','tokio-rs/tokio','prometheus/prometheus',
         'vuejs/core','google/closure-compiler','google/googletest','pallets/flask','google/cadvisor',
         'google/go-github','google/zx','google/brotli','google/guava')
rows = query("""select r.id, r.full_name, r.host, count(*) n
                from commit c join repo r on r.id=c.repo_id
                where c.pair_eligible group by 1,2,3 order by n desc""")
by = {r['full_name']: r for r in rows}
for n in names:
    hits=[r for r in rows if r['full_name'].endswith(n.split('/')[-1])]
    print(n, hits[:3])
