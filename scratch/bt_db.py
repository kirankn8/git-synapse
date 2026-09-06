from git_synapse.db.engine import query
print(query("select count(*) c from repo")[0])
rows = query("""select r.id, r.full_name, r.host, count(*) n
                from commit c join repo r on r.id=c.repo_id
                where c.pair_eligible group by 1,2,3 order by n limit 40""")
for r in rows: print(r)
