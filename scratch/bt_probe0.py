from git_synapse.stats.registry import DEFAULT_MEASURE, resolve
print("DEFAULT_MEASURE", DEFAULT_MEASURE)
s = resolve(DEFAULT_MEASURE)
print(s.key, s.label, s.rare_item_bias)
import numpy as np
from git_synapse.stats.contingency import Contingency
c = Contingency.from_counts(np.array([2.0]), np.array([5.0]), np.array([4.0]), 100.0)
print("pba", s.compute(c))
