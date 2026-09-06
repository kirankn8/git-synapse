from git_synapse.analysis.backtest import wilson
def show(tag, h, n):
    lo, hi = wilson(h, n)
    print(f"{tag}: {h}/{n} = {h/n:.4%}  CI {lo:.3%}-{hi:.3%}")

N = 769484
print("README headline: 769,484 prompts, 173,034 commits")
# Apprentice 53.0% CI 52.9-53.1
for p in (0.530,):
    show("Apprentice@53.0%", round(p*N), N)
show("Intern@45.2%", round(0.452*N), N)
show("Tourist@40.6%", round(0.406*N), N)
show("confidence_ab@61.6%", round(0.616*N), N)
show("NewHire 166/400", 166, 400)
show("NewHire 41.5% of 400", round(0.415*400), 400)
print()
print("hard prompts: README says 361,413 of 769,484")
print("  implied Apprentice hit rate =", 1 - 361413/769484)
print("  README Apprentice           = 0.530")
print()
print("unaided: 141 prompts, P(B|A) 49.6% (41.5%-57.8%)")
show("  unaided 70/141", 70, 141)
show("  unaided 69/141", 69, 141)
print()
print("lift 61.6/53.0 =", 0.616/0.530)
