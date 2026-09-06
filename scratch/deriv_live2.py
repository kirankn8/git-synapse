import sys; sys.path.insert(0,"/work/scratch")
from deriv_lib import conn, dump
c = conn(autocommit=True); c.execute("SET statement_timeout='900s'")
dump(c,"""SELECT count(*) FROM file f WHERE (f.change_count>0 OR f.pair_change_count>0)
          AND NOT EXISTS (SELECT 1 FROM commit_file cf WHERE cf.file_id=f.id)""",None,
     "S1 files with stale counts but zero commit_file rows")
dump(c,"""SELECT count(*) FROM directory d WHERE (d.file_count>0 OR d.change_count>0)
          AND NOT EXISTS (SELECT 1 FROM file_directory fd WHERE fd.dir_id=d.id)""",None,
     "S2 directories with stale counts but no files")
dump(c,"""SELECT r.full_name, f.path, f.change_count, f.pair_change_count, f.last_change_at
          FROM file f JOIN repo r ON r.id=f.repo_id
          WHERE f.change_count>0 AND NOT EXISTS (SELECT 1 FROM commit_file cf WHERE cf.file_id=f.id)
          ORDER BY f.change_count DESC LIMIT 10""",None,"S1 examples")
dump(c,"""SELECT r.full_name, d.path, d.file_count, d.change_count
          FROM directory d JOIN repo r ON r.id=d.repo_id
          WHERE d.file_count>0 AND NOT EXISTS (SELECT 1 FROM file_directory fd WHERE fd.dir_id=d.id)
          ORDER BY d.change_count DESC LIMIT 10""",None,"S2 examples")
dump(c,"""SELECT count(*) FROM file_pair_metric m
          LEFT JOIN file fa ON fa.id=m.file_a_id LEFT JOIN file fb ON fb.id=m.file_b_id
          WHERE fa.id IS NULL OR fb.id IS NULL""",None,"S3 metric rows pointing at deleted files")
dump(c,"SELECT min(w_ab), max(w_ab), count(*) FILTER (WHERE w_ab=0) FROM file_pair",None,"W1 w_ab range / underflow-to-zero count")
dump(c,"SELECT count(*) FROM file_pair WHERE w_ab=0 AND n_ab>0",None,"W2 pairs whose recency weight underflowed to exactly 0")
dump(c,"SELECT min(committed_at), max(committed_at) FROM commit",None,"commit time range")
