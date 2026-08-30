"""Generate the benchmark chart, one SVG per GitHub colour scheme."""
import pathlib

# repo, commits, baseline %, P(B|A) %, lift
DATA = [
    ("google (38 repos)", 89121, 9.0, 61.3, 6.84),
    ("scikit-learn", 30873, 14.2, 55.1, 3.88),
    ("django",       33992, 14.8, 49.8, 3.36),
    ("requests",      4856, 40.8, 64.6, 1.58),
    ("pytest",       13071, 34.7, 53.4, 1.54),
    ("fastapi",       7594, 28.3, 41.2, 1.45),
    ("flask",         3821, 48.6, 60.5, 1.24),
]

THEMES = {
    "light": dict(text="#1f2328", dim="#57606a", faint="#8c959f",
                  base="#d0d7de", ours="#0d9488", rule="#d8dee4"),
    "dark":  dict(text="#e6edf3", dim="#9198a1", faint="#6e7681",
                  base="#30363d", ours="#2dd4bf", rule="#30363d"),
}

W, LEFT, RIGHT = 820, 132, 96
ROW, BAR, GAP, TOP = 54, 15, 5, 74
PLOT = W - LEFT - RIGHT


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;")


def build(t):
    h = TOP + ROW * len(DATA) + 26
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
         f'viewBox="0 0 {W} {h}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">']
    o.append(f'<text x="0" y="20" font-size="15" font-weight="700" fill="{t["text"]}">'
             'Would it have named the right files?</text>')
    o.append(f'<text x="0" y="40" font-size="12" fill="{t["dim"]}">'
             '168,620 predictions across 94,872 commits — each scored only against earlier history</text>')
    # legend
    o.append(f'<rect x="0" y="54" width="10" height="10" rx="2" fill="{t["base"]}"/>'
             f'<text x="16" y="63" font-size="11.5" fill="{t["dim"]}">guessing the busiest files</text>')
    o.append(f'<rect x="196" y="54" width="10" height="10" rx="2" fill="{t["ours"]}"/>'
             f'<text x="212" y="63" font-size="11.5" fill="{t["dim"]}">Git Synapse — P(B|A)</text>')

    for i, (name, commits, base, ours, lift) in enumerate(DATA):
        y = TOP + i * ROW
        o.append(f'<line x1="0" y1="{y - 9}" x2="{W}" y2="{y - 9}" stroke="{t["rule"]}" stroke-width="1"/>')
        o.append(f'<text x="0" y="{y + 13}" font-size="13" font-weight="600" fill="{t["text"]}">{esc(name)}</text>')
        o.append(f'<text x="0" y="{y + 28}" font-size="10.5" fill="{t["faint"]}">{commits:,} commits</text>')

        bw = PLOT * base / 100.0
        o.append(f'<rect x="{LEFT}" y="{y}" width="{bw:.1f}" height="{BAR}" rx="3" fill="{t["base"]}"/>')
        o.append(f'<text x="{LEFT + bw + 7:.1f}" y="{y + 12}" font-size="11" fill="{t["dim"]}">{base:.1f}%</text>')

        ow = PLOT * ours / 100.0
        y2 = y + BAR + GAP
        o.append(f'<rect x="{LEFT}" y="{y2}" width="{ow:.1f}" height="{BAR}" rx="3" fill="{t["ours"]}"/>')
        o.append(f'<text x="{LEFT + ow + 7:.1f}" y="{y2 + 12}" font-size="11" font-weight="700" '
                 f'fill="{t["ours"]}">{ours:.1f}%</text>')

        o.append(f'<text x="{W - 6}" y="{y + 22}" font-size="17" font-weight="700" text-anchor="end" '
                 f'fill="{t["ours"]}">{lift:.2f}x</text>')

    o.append(f'<text x="0" y="{h - 6}" font-size="10.5" fill="{t["faint"]}">'
             'Share of predictions where at least one correct file appeared in the top 5. '
             'No confidence intervals overlap.</text>')
    o.append('</svg>')
    return "\n".join(o)


for name, t in THEMES.items():
    pathlib.Path(f"docs/benchmark-{name}.svg").write_text(build(t))
    print(f"docs/benchmark-{name}.svg")
