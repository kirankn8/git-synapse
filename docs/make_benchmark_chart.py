"""Generate the benchmark chart, one SVG per GitHub colour scheme.

The chart is a union, not a race. An agent already finds a file's test, its
siblings and its directory for free, and those rules are strong. The only
honest question is what history adds *on top* of them, so each bar is the
Apprentice's coverage plus the share of the remainder Git Synapse recovers.
"""
import pathlib

#: repo, language, commits replayed, Apprentice hit rate, share of the prompts
#: the Apprentice missed that P(B|A) recovered. Every figure measured over all
#: prompts -- none of these columns is sampled.
DATA = [
    ("google (38 repos)", "mixed",  89_121, 56.9, 43.3),
    ("flatbuffers",       "C++",     3_147, 43.8, 57.8),
    ("pytype",            "Python",  6_184, 47.3, 51.9),
    ("osv-scanner",       "Go",      2_005, 47.7, 50.8),
    ("closure-compiler",  "Java",   20_399, 54.4, 47.7),
    ("go-github",         "Go",      3_000, 76.8, 44.3),
    ("guava",             "Java",    7_501, 76.6, 34.5),
]

THEMES = {
    "light": dict(text="#1f2328", dim="#57606a", faint="#8c959f",
                  base="#d0d7de", ours="#0d9488", rule="#d8dee4"),
    "dark":  dict(text="#e6edf3", dim="#9198a1", faint="#6e7681",
                  base="#30363d", ours="#2dd4bf", rule="#30363d"),
}

W, LEFT, RIGHT = 820, 168, 74
ROW, BAR, TOP = 40, 18, 78
PLOT = W - LEFT - RIGHT


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;")


def build(t):
    h = TOP + ROW * len(DATA) + 34
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
         f'viewBox="0 0 {W} {h}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">']
    o.append(f'<text x="0" y="20" font-size="15" font-weight="700" fill="{t["text"]}">'
             'What history adds to what an agent already finds for free</text>')
    o.append(f'<text x="0" y="40" font-size="12" fill="{t["dim"]}">'
             'Every prediction scored only against the commits that preceded it</text>')
    o.append(f'<rect x="0" y="56" width="10" height="10" rx="2" fill="{t["base"]}"/>'
             f'<text x="16" y="65" font-size="11.5" fill="{t["dim"]}">'
             'Apprentice &#8212; the file&#8217;s test, then its folder</text>')
    o.append(f'<rect x="310" y="56" width="10" height="10" rx="2" fill="{t["ours"]}"/>'
             f'<text x="326" y="65" font-size="11.5" fill="{t["dim"]}">'
             'recovered by Git Synapse from history</text>')

    for i, (name, lang, prompts, base, recovered) in enumerate(DATA):
        y = TOP + i * ROW
        added = (100.0 - base) * recovered / 100.0
        o.append(f'<line x1="0" y1="{y - 8}" x2="{W}" y2="{y - 8}" stroke="{t["rule"]}" stroke-width="1"/>')
        o.append(f'<text x="0" y="{y + 9}" font-size="12.5" font-weight="600" fill="{t["text"]}">{esc(name)}</text>')
        o.append(f'<text x="0" y="{y + 23}" font-size="10" fill="{t["faint"]}">'
                 f'{esc(lang)} &#183; {prompts:,} commits</text>')

        bw = PLOT * base / 100.0
        aw = PLOT * added / 100.0
        o.append(f'<rect x="{LEFT}" y="{y}" width="{bw:.1f}" height="{BAR}" rx="3" fill="{t["base"]}"/>')
        o.append(f'<rect x="{LEFT + bw:.1f}" y="{y}" width="{aw:.1f}" height="{BAR}" fill="{t["ours"]}"/>')
        o.append(f'<text x="{LEFT + 7}" y="{y + 13}" font-size="10.5" fill="{t["dim"]}">{base:.1f}%</text>')
        o.append(f'<text x="{LEFT + bw + 5:.1f}" y="{y + 13}" font-size="10.5" font-weight="700" '
                 f'fill="#ffffff">+{added:.1f}</text>')
        o.append(f'<text x="{W - 4}" y="{y + 14}" font-size="14.5" font-weight="700" text-anchor="end" '
                 f'fill="{t["ours"]}">{base + added:.1f}%</text>')

    o.append(f'<text x="0" y="{h - 10}" font-size="10" fill="{t["faint"]}">'
             'Share of predictions where at least one file that really changed appeared in the top 5. '
             'On guava the Apprentice alone beats every measure &#8212;</text>')
    o.append(f'<text x="0" y="{h - 0}" font-size="10" fill="{t["faint"]}">'
             'a well-organised codebase is one history has least to add to.</text>')
    o.append('</svg>')
    return "\n".join(o)


for name, t in THEMES.items():
    pathlib.Path(f"docs/benchmark-{name}.svg").write_text(build(t))
    print(f"docs/benchmark-{name}.svg")
