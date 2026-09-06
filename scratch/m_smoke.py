import os, sys
print(sys.version)
from git_synapse.analysis import manifests, mining, predict, depbump
print("imports ok")
print("ecosystems:", len(manifests.ECOSYSTEMS), "distinct names:", len({e.name for e in manifests.ECOSYSTEMS}))
print(sorted({e.name for e in manifests.ECOSYSTEMS}))
