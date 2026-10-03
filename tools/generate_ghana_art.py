"""Generate ADUFARMS' original Ghanaian banner artwork (SVG) into static/images/ghana/.

    python tools/generate_ghana_art.py

Everything is drawn from scratch here (kente weave, Adinkra symbols, maize, baobab, sacks, trucks, market stalls,
the Black Star, cedi coins), so the artwork is licence-free, tiny, and sharp at any size. Composition rule: the LEFT
~40% of every banner stays calm and dark because page titles are laid over it; the detail lives on the right.
"""
import math
import random
from pathlib import Path

W, H = 1600, 320
RED, GOLD, GREEN, BLACK = "#CE1126", "#FCD116", "#006B3F", "#111111"
OUT = Path(__file__).resolve().parent.parent / "static" / "images" / "ghana"


# ---------------------------------------------------------------------------------------------- building blocks
def defs():
    """Shared gradients and the kente weave pattern."""
    return f"""
<defs>
  <pattern id="kente" width="176" height="48" patternUnits="userSpaceOnUse" patternTransform="scale(.73)">
    <rect width="176" height="48" fill="{BLACK}"/>
    <rect y="0" width="176" height="7" fill="{GOLD}"/><rect y="7" width="176" height="4" fill="{RED}"/><rect y="11" width="176" height="5" fill="{GREEN}"/>
    <rect y="37" width="176" height="5" fill="{GREEN}"/><rect y="42" width="176" height="4" fill="{RED}"/><rect y="46" width="176" height="2" fill="{GOLD}"/>
    <g transform="translate(0,16)">
      <polygon points="22,16 44,0 66,16 44,32" fill="{GOLD}"/><polygon points="22,16 44,6 66,16 44,26" fill="{RED}"/><polygon points="36,16 44,10 52,16 44,22" fill="{BLACK}"/>
      <rect x="66" y="6" width="22" height="20" fill="{GREEN}"/><rect x="71" y="11" width="12" height="10" fill="{GOLD}"/>
      <polygon points="110,16 132,0 154,16 132,32" fill="{GOLD}"/><polygon points="110,16 132,6 154,16 132,26" fill="{RED}"/><polygon points="124,16 132,10 140,16 132,22" fill="{BLACK}"/>
      <rect x="154" y="6" width="22" height="20" fill="{GREEN}"/><rect x="159" y="11" width="12" height="10" fill="{GOLD}"/>
      <rect x="0" y="6" width="22" height="20" fill="{GREEN}"/><rect x="5" y="11" width="12" height="10" fill="{GOLD}"/>
      <rect x="88" y="6" width="22" height="20" fill="{RED}"/><rect x="93" y="11" width="12" height="10" fill="{BLACK}"/>
    </g>
  </pattern>
  <radialGradient id="glow" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#fff6c2" stop-opacity=".95"/><stop offset=".35" stop-color="{GOLD}" stop-opacity=".55"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient>
  <linearGradient id="sack" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#e9d3a1"/><stop offset="1" stop-color="#c9a96a"/></linearGradient>
  <linearGradient id="cob" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffd54a"/><stop offset="1" stop-color="#e9a316"/></linearGradient>
  <filter id="soft"><feGaussianBlur stdDeviation="1.2"/></filter>
</defs>"""


def sky(top, mid, bottom):
    return (f'<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/><stop offset=".55" stop-color="{mid}"/>'
            f'<stop offset="1" stop-color="{bottom}"/></linearGradient></defs><rect width="{W}" height="{H}" fill="url(#sky)"/>')


def sun(cx, cy, r, rays=True):
    out = f'<circle cx="{cx}" cy="{cy}" r="{r * 3.2}" fill="url(#glow)"/>'
    if rays:
        for i in range(16):
            a = math.radians(i * 22.5)
            x1, y1 = cx + math.cos(a) * (r * 1.25), cy + math.sin(a) * (r * 1.25)
            x2, y2 = cx + math.cos(a) * (r * (1.9 if i % 2 else 1.6)), cy + math.sin(a) * (r * (1.9 if i % 2 else 1.6))
            out += f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{GOLD}" stroke-width="3.5" stroke-linecap="round" opacity=".55"/>'
    return out + f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#fff3b0"/><circle cx="{cx}" cy="{cy}" r="{r * .82}" fill="{GOLD}"/>'


def hills(color, base, amp, seed, opacity=1.0, x0=0):
    rnd = random.Random(seed)
    pts, x = [], x0 - 50
    y = base
    d = f"M{x0 - 50},{H} L{x0 - 50},{base}"
    while x < W + 100:
        x += rnd.randint(140, 260)
        y = base + rnd.randint(-amp, amp)
        d += f" Q{x - 90},{y - amp // 2} {x},{y}"
    d += f" L{W + 100},{H} Z"
    return f'<path d="{d}" fill="{color}" opacity="{opacity}"/>'


def maize(x, base_y, h, s=1.0, tone=0):
    """One maize plant: stalk, alternating leaves, a golden cob in its husk, and a tassel."""
    stalk = ["#2f7d32", "#378a39", "#2a6f2e"][tone % 3]
    leaf = ["#3f9a43", "#46a84a", "#358a3a"][tone % 3]
    leaf2 = ["#2f7d32", "#36883a", "#286b2c"][tone % 3]
    out = [f'<path d="M{x},{base_y} C{x + 4 * s},{base_y - h * .4} {x - 4 * s},{base_y - h * .7} {x},{base_y - h}" stroke="{stalk}" stroke-width="{5 * s:.1f}" fill="none" stroke-linecap="round"/>']
    for i in range(6):
        y = base_y - h * (0.22 + 0.12 * i)
        d = 1 if i % 2 == 0 else -1
        length = (150 - i * 10) * s
        out.append(f'<path d="M{x},{y:.1f} Q{x + d * length * .5:.1f},{y - 46 * s:.1f} {x + d * length:.1f},{y + 26 * s:.1f} Q{x + d * length * .45:.1f},{y - 8 * s:.1f} {x},{y:.1f} Z" fill="{leaf if i % 2 else leaf2}"/>')
    cy = base_y - h * 0.52
    out.append(f'<g transform="translate({x + 9 * s:.1f},{cy:.1f}) rotate(14)"><ellipse rx="{13 * s:.1f}" ry="{36 * s:.1f}" fill="url(#cob)"/>'
               f'<path d="M{-13 * s:.1f},{-10 * s:.1f} Q0,{-30 * s:.1f} {13 * s:.1f},{-10 * s:.1f}" stroke="#b07a10" stroke-width="{1.5 * s:.1f}" fill="none" opacity=".5"/>'
               f'<path d="M{-14 * s:.1f},{30 * s:.1f} Q{-4 * s:.1f},{-18 * s:.1f} 0,{-40 * s:.1f} Q{6 * s:.1f},{-8 * s:.1f} {16 * s:.1f},{32 * s:.1f} Z" fill="#8fbf5a" opacity=".92"/></g>')
    top = base_y - h
    for k in range(-3, 4):
        out.append(f'<path d="M{x},{top:.1f} q{k * 7 * s:.1f},{-26 * s:.1f} {k * 12 * s:.1f},{-38 * s:.1f}" stroke="#d9b650" stroke-width="{1.6 * s:.1f}" fill="none" stroke-linecap="round"/>')
    return "".join(out)


def field(x_from, x_to, rows, base, seed, scale=1.0):
    """Rows of maize, back rows small and pale, front rows big and dark."""
    rnd = random.Random(seed)
    out = []
    for r in range(rows):
        depth = r / max(rows - 1, 1)
        y = base + r * 34 * scale
        size = (150 + depth * 120) * scale
        step = (70 + depth * 40) * scale
        x = x_from + rnd.randint(0, 30)
        while x < x_to:
            out.append(maize(x, y + rnd.randint(-6, 6), size * rnd.uniform(.92, 1.08), s=(.55 + depth * .5) * scale, tone=rnd.randint(0, 2)))
            x += step * rnd.uniform(.9, 1.15)
        out.append(f'<rect x="{x_from}" y="{y + 4:.1f}" width="{x_to - x_from}" height="{H}" fill="#14351f" opacity="{.12 + depth * .1:.2f}"/>')
    return "".join(out)


def baobab(x, y, s=1.0, color="#20140f"):
    return (f'<g transform="translate({x},{y}) scale({s})" fill="{color}">'
            '<path d="M-34,0 C-40,-60 -26,-120 -16,-150 L16,-150 C26,-120 40,-60 34,0 Z"/>'
            '<path d="M-14,-146 C-70,-176 -110,-180 -150,-215 M-14,-150 C-50,-200 -70,-232 -62,-262 M0,-150 C0,-204 6,-240 0,-276 '
            'M14,-150 C50,-200 70,-232 62,-262 M14,-146 C70,-176 110,-180 150,-215" stroke="' + color + '" stroke-width="11" fill="none" stroke-linecap="round"/>'
            '<g opacity=".95"><ellipse cx="-150" cy="-222" rx="50" ry="22"/><ellipse cx="-62" cy="-270" rx="46" ry="22"/><ellipse cx="0" cy="-284" rx="50" ry="22"/>'
            '<ellipse cx="62" cy="-270" rx="46" ry="22"/><ellipse cx="150" cy="-222" rx="50" ry="22"/></g></g>')


def kente(y=None, h=35):
    y = H - h if y is None else y
    return (f'<g><rect x="0" y="{y}" width="{W}" height="{h}" fill="url(#kente)"/>'
            f'<rect x="0" y="{y}" width="{W}" height="3" fill="#000" opacity=".35"/></g>')


def sack(x, y, s=1.0, rot=0, label=True):
    g = (f'<g transform="translate({x},{y}) rotate({rot}) scale({s})">'
         '<path d="M-46,0 C-54,-30 -52,-70 -40,-92 L-22,-102 L-30,-116 Q0,-104 30,-116 L22,-102 L40,-92 C52,-70 54,-30 46,0 Q0,12 -46,0 Z" fill="url(#sack)" stroke="#a68545" stroke-width="2"/>'
         '<path d="M-30,-116 Q0,-96 30,-116" stroke="#8a6b2f" stroke-width="3" fill="none"/><path d="M-22,-102 Q0,-92 22,-102" stroke="#a68545" stroke-width="2" fill="none" stroke-dasharray="3 4"/>')
    if label:
        g += f'<rect x="-26" y="-74" width="52" height="32" rx="4" fill="#fffaf0" opacity=".92"/><path d="M-17,-50 L-10,-66 L0,-52 L10,-66 L17,-50" stroke="{GREEN}" stroke-width="3.5" fill="none" stroke-linejoin="round"/>'
        g += f'<rect x="-26" y="-45" width="52" height="3" fill="{RED}"/>'
    return g + "</g>"


def sack_pile(x, y, rows=3, s=1.0, cols_top=2, seed=1):
    rnd = random.Random(seed)
    out = []
    for r in range(rows):
        cols = cols_top + (rows - 1 - r)
        for c in range(cols):
            out.append(sack(x + (c - (cols - 1) / 2) * 92 * s, y - r * 62 * s, s, rnd.uniform(-3, 3), label=(c + r) % 2 == 0))
    return "".join(out)


def truck(x, y, s=1.0):
    g = f'<g transform="translate({x},{y}) scale({s})">'
    g += '<rect x="-150" y="-66" width="250" height="14" rx="3" fill="#3b2a1d"/>'
    for i in range(3):
        for j in range(2 - (i == 2)):
            g += sack(-112 + i * 78 + j * 0, -66 - j * 58, .62, (i - 1) * 2, label=(i + j) % 2 == 0)
    g += f'<path d="M104,-66 L104,-130 L160,-130 L190,-96 L190,-52 L104,-52 Z" fill="{RED}"/><path d="M116,-122 L156,-122 L176,-98 L116,-98 Z" fill="#cfe9f3" opacity=".9"/>'
    g += f'<rect x="104" y="-52" width="86" height="8" fill="{GOLD}"/><rect x="-150" y="-44" width="340" height="10" rx="3" fill="#222"/>'
    for cx in (-110, -40, 130):
        g += f'<circle cx="{cx}" cy="-28" r="24" fill="#191919"/><circle cx="{cx}" cy="-28" r="11" fill="#9aa0a6"/><circle cx="{cx}" cy="-28" r="4" fill="#191919"/>'
    return g + "</g>"


def umbrella(x, y, s=1.0, colors=(RED, GOLD, GREEN, GOLD, RED)):
    """Market umbrella with kente-coloured gores over a stall table."""
    g = f'<g transform="translate({x},{y}) scale({s})">'
    g += '<rect x="-3" y="-150" width="6" height="150" fill="#3b2a1d"/>'
    n = len(colors)
    for i, col in enumerate(colors):
        x1 = -110 + i * (220 / n)
        x2 = -110 + (i + 1) * (220 / n)
        g += f'<path d="M0,-176 L{x1:.1f},-118 Q{(x1 + x2) / 2:.1f},-104 {x2:.1f},-118 Z" fill="{col}"/>'
    g += '<circle cx="0" cy="-178" r="6" fill="#3b2a1d"/>'
    g += f'<rect x="-120" y="-44" width="240" height="10" rx="3" fill="#7a4a24"/><rect x="-108" y="-34" width="10" height="34" fill="#5d3a1c"/><rect x="98" y="-34" width="10" height="34" fill="#5d3a1c"/>'
    return g + "</g>"


def black_star(cx, cy, r, fill=BLACK, stroke=GOLD):
    pts = []
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rr = r if i % 2 == 0 else r * 0.382
        pts.append(f"{cx + math.cos(a) * rr:.1f},{cy + math.sin(a) * rr:.1f}")
    return f'<polygon points="{" ".join(pts)}" fill="{fill}" stroke="{stroke}" stroke-width="3" stroke-linejoin="round"/>'


def cedi_coin(cx, cy, r):
    return (f'<g><circle cx="{cx}" cy="{cy}" r="{r}" fill="{GOLD}" stroke="#b8860b" stroke-width="3"/><circle cx="{cx}" cy="{cy}" r="{r * .78}" fill="none" stroke="#b8860b" stroke-width="2" opacity=".7"/>'
            f'<path d="M{cx + r * .3:.1f},{cy - r * .42:.1f} A{r * .42:.1f},{r * .5:.1f} 0 1 0 {cx + r * .3:.1f},{cy + r * .42:.1f}" stroke="#7a5200" stroke-width="{r * .14:.1f}" fill="none" stroke-linecap="round"/>'
            f'<path d="M{cx - r * .05:.1f},{cy - r * .68:.1f} L{cx - r * .12:.1f},{cy + r * .68:.1f}" stroke="#7a5200" stroke-width="{r * .1:.1f}" stroke-linecap="round"/></g>')


def adinkra(kind, cx, cy, s=1.0, color=GOLD, opacity=.3, width=5):
    """A few Adinkra-inspired symbols, simplified line-art."""
    st = f'stroke="{color}" stroke-width="{width}" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    g = f'<g transform="translate({cx},{cy}) scale({s})" opacity="{opacity}">'
    if kind == "akoma":        # the heart: patience and tolerance
        g += f'<path d="M0,44 C-70,-6 -58,-58 -28,-58 C-12,-58 0,-44 0,-34 C0,-44 12,-58 28,-58 C58,-58 70,-6 0,44 Z" {st}/>'
    elif kind == "dwennimmen":  # ram's horns: humility with strength
        g += f'<path d="M-6,-6 C-50,-40 -74,-10 -56,18 C-44,36 -22,28 -26,10 C-28,0 -40,4 -36,14 M6,-6 C50,-40 74,-10 56,18 C44,36 22,28 26,10 C28,0 40,4 36,14 M-56,44 L56,44 M0,-6 L0,44" {st}/>'
    elif kind == "aya":         # the fern: endurance and resourcefulness
        g += f'<path d="M0,56 L0,-56" {st}/>' + "".join(f'<path d="M0,{y} L-30,{y - 18} M0,{y} L30,{y - 18}" {st}/>' for y in range(40, -46, -20))
    elif kind == "nyame":       # Nyame Dua: the altar of God
        g += f'<path d="M0,56 L0,-10 M0,-10 L-38,-44 M0,-10 L38,-44 M-38,-44 C-38,-66 -14,-66 -14,-44 M38,-44 C38,-66 14,-66 14,-44 M-20,56 L20,56" {st}/>'
    elif kind == "sankofa":     # go back and fetch it
        g += f'<path d="M0,46 C-66,-2 -50,-54 -24,-54 C-10,-54 0,-42 0,-32 C0,-42 10,-54 24,-54 C50,-54 66,-2 0,46 Z" {st}/><path d="M-20,-30 C-42,-26 -44,0 -26,6 M20,-30 C42,-26 44,0 26,6" {st}/>'
    return g + "</g>"


def adinkra_row(x, y, kinds, gap=120, **kw):
    return "".join(adinkra(k, x + i * gap, y, **kw) for i, k in enumerate(kinds))


def stars(seed, n=34, region=(0, 0, W, 170)):
    rnd = random.Random(seed)
    x0, y0, x1, y1 = region
    return "".join(f'<circle cx="{rnd.randint(x0, x1)}" cy="{rnd.randint(y0, y1)}" r="{rnd.choice([1.2, 1.6, 2.2])}" fill="#fff" opacity="{rnd.uniform(.25, .8):.2f}"/>' for _ in range(n))


def person(x, y, s=1.0, top=RED, skin="#6b4226"):
    """A simple, friendly person icon (head and shoulders)."""
    return (f'<g transform="translate({x},{y}) scale({s})"><circle cx="0" cy="-58" r="20" fill="{skin}"/>'
            f'<path d="M-40,0 C-40,-34 -22,-40 0,-40 C22,-40 40,-34 40,0 Z" fill="{top}"/><path d="M-10,-40 L0,-26 L10,-40" fill="{GOLD}"/></g>')


def phone(x, y, s=1.0, rot=-8):
    return (f'<g transform="translate({x},{y}) rotate({rot}) scale({s})"><rect x="-58" y="-112" width="116" height="224" rx="18" fill="#10251a" stroke="#0a170f" stroke-width="3"/>'
            f'<rect x="-50" y="-96" width="100" height="190" rx="10" fill="#f6fbf7"/><rect x="-50" y="-96" width="100" height="42" rx="10" fill="{GREEN}"/>'
            f'<text x="0" y="-68" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-size="15" font-weight="700" fill="#fff">MoMo</text>'
            f'<rect x="-38" y="-40" width="76" height="14" rx="7" fill="#dfeee4"/><rect x="-38" y="-18" width="52" height="14" rx="7" fill="#dfeee4"/>'
            f'<rect x="-38" y="6" width="76" height="34" rx="9" fill="{GOLD}"/><path d="M-14,23 L-4,13 L4,23 L14,13" stroke="{BLACK}" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="0" cy="75" r="11" fill="{GREEN}" opacity=".85"/><path d="M-5,75 L-1,79 L6,71" stroke="#fff" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/></g>')


def document(x, y, s=1.0, rot=6, tone="#fffaf0"):
    g = f'<g transform="translate({x},{y}) rotate({rot}) scale({s})"><rect x="-80" y="-110" width="160" height="220" rx="8" fill="{tone}" stroke="#cbbf9e" stroke-width="2"/>'
    g += f'<rect x="-80" y="-110" width="160" height="34" rx="8" fill="{GREEN}"/><text x="-62" y="-86" font-family="Arial,Helvetica,sans-serif" font-size="15" font-weight="700" fill="#fff">INVOICE</text>'
    for i in range(6):
        g += f'<rect x="-62" y="{-56 + i * 24}" width="{124 - (i % 3) * 26}" height="8" rx="4" fill="#d9d2bd"/>'
    g += f'<rect x="8" y="62" width="62" height="26" rx="6" fill="{GOLD}"/>'
    g += f'<circle cx="-40" cy="74" r="22" fill="none" stroke="{RED}" stroke-width="4" opacity=".85"/><path d="M-50,74 L-43,82 L-28,64" stroke="{RED}" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round" opacity=".85"/>'
    return g + "</g>"


def bars(x, y, heights, w=46, gap=22, colors=(GREEN, GOLD, RED, GREEN, GOLD, RED, GREEN)):
    out = []
    for i, h in enumerate(heights):
        out.append(f'<rect x="{x + i * (w + gap)}" y="{y - h}" width="{w}" height="{h}" rx="7" fill="{colors[i % len(colors)]}" opacity=".92"/>')
    return "".join(out)


def warehouse(x, y, s=1.0):
    g = f'<g transform="translate({x},{y}) scale({s})">'
    g += f'<path d="M-230,0 L-230,-150 L0,-215 L230,-150 L230,0 Z" fill="#7b5a3a"/><path d="M-250,-146 L0,-226 L250,-146 L250,-128 L0,-208 L-250,-128 Z" fill="{RED}"/>'
    g += f'<rect x="-150" y="-110" width="300" height="110" fill="#3a2a1b"/>'
    for i in range(5):
        g += f'<rect x="{-150 + i * 60}" y="-110" width="60" height="110" fill="none" stroke="#5d452c" stroke-width="3"/>'
    g += f'<path d="M-200,-150 L0,-204 L200,-150" stroke="{GOLD}" stroke-width="4" fill="none"/>'
    g += f'<circle cx="0" cy="-170" r="13" fill="{GOLD}"/>'
    return g + "</g>"


def shield(x, y, s=1.0):
    return (f'<g transform="translate({x},{y}) scale({s})"><path d="M0,-110 L86,-76 C86,-10 56,60 0,104 C-56,60 -86,-10 -86,-76 Z" fill="{GREEN}" stroke="{GOLD}" stroke-width="6"/>'
            f'<path d="M0,-84 L62,-60 C62,-14 42,36 0,72 Z" fill="#0b8a53" opacity=".55"/><path d="M-30,0 L-8,24 L34,-24" stroke="#fff" stroke-width="12" fill="none" stroke-linecap="round" stroke-linejoin="round"/></g>')


def magnifier(x, y, s=1.0):
    return (f'<g transform="translate({x},{y}) scale({s})"><circle cx="0" cy="0" r="54" fill="#cfe9f3" opacity=".35" stroke="{GOLD}" stroke-width="10"/>'
            f'<path d="M40,40 L86,86" stroke="{GOLD}" stroke-width="16" stroke-linecap="round"/><path d="M-30,-18 A40,40 0 0 1 10,-44" stroke="#fff" stroke-width="6" fill="none" stroke-linecap="round" opacity=".8"/></g>')


def basket(x, y, s=1.0):
    g = f'<g transform="translate({x},{y}) scale({s})"><path d="M-70,-50 L70,-50 L56,0 L-56,0 Z" fill="#b07a3c"/>'
    for i in range(-3, 4):
        g += f'<path d="M{i * 18},-50 L{i * 14},0" stroke="#8a5a26" stroke-width="3"/>'
    g += f'<path d="M-70,-50 L70,-50" stroke="#8a5a26" stroke-width="6"/>'
    for i, (cx, cy) in enumerate([(-44, -62), (-14, -72), (18, -66), (46, -60), (-28, -84), (12, -90)]):
        g += f'<ellipse cx="{cx}" cy="{cy}" rx="11" ry="26" transform="rotate({(i - 2) * 9} {cx} {cy})" fill="url(#cob)"/>'
    return g + "</g>"


def frame(inner, name):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" preserveAspectRatio="xMidYMid slice" role="img" '
            f'aria-label="ADUFARMS {name} banner">{defs()}{inner}</svg>')


def left_shade():
    """Keeps the left side dark and calm so titles stay readable on top."""
    return (f'<defs><linearGradient id="shade" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#06210f" stop-opacity=".78"/>'
            f'<stop offset=".42" stop-color="#06210f" stop-opacity=".38"/><stop offset=".7" stop-color="#06210f" stop-opacity="0"/></linearGradient></defs>'
            f'<rect width="{W}" height="{H}" fill="url(#shade)"/>')


# ---------------------------------------------------------------------------------------------- scenes
# Canvas is 1600 x 320 (5:1). Kente band occupies y 285-320; ground line ~276; key content lives in y 90-280 so it
# survives the tighter crops (the short page banners show roughly the bottom 75%).
GROUND = 276


def restore_arrow(cx, cy, r, color=GOLD):
    a0, a1 = math.radians(-50), math.radians(215)
    x0, y0 = cx + math.cos(a0) * r, cy + math.sin(a0) * r
    x1, y1 = cx + math.cos(a1) * r, cy + math.sin(a1) * r
    tip = (x1, y1)
    ta = a1 + math.pi / 2
    head = f"{tip[0] + math.cos(ta) * 15:.1f},{tip[1] + math.sin(ta) * 15:.1f} {tip[0] + math.cos(ta + 2.45) * 15:.1f},{tip[1] + math.sin(ta + 2.45) * 15:.1f} {tip[0] + math.cos(ta - 2.45) * 15:.1f},{tip[1] + math.sin(ta - 2.45) * 15:.1f}"
    return (f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 1 0 {x1:.1f},{y1:.1f}" stroke="{color}" stroke-width="9" fill="none" stroke-linecap="round" opacity=".9"/>'
            f'<polygon points="{head}" fill="{color}" opacity=".9"/>')


def banknote(x, y, w=118, h=58, rot=0):
    return (f'<g transform="translate({x},{y}) rotate({rot})"><rect x="{-w / 2}" y="{-h / 2}" width="{w}" height="{h}" rx="7" fill="#2f9e5f" stroke="{GOLD}" stroke-width="3"/>'
            f'<rect x="{-w / 2 + 7}" y="{-h / 2 + 7}" width="{w - 14}" height="{h - 14}" rx="4" fill="none" stroke="#bff0cf" stroke-width="1.6" opacity=".8"/>'
            f'<circle cx="0" cy="0" r="{h * .28:.1f}" fill="{GOLD}" opacity=".95"/>{black_star(0, 0, h * .2, BLACK, BLACK)}'
            f'<path d="M{-w / 2 + 12},{h / 2 - 12} h{w * .22:.1f} M{w / 2 - 12 - w * .22:.1f},{-h / 2 + 12} h{w * .22:.1f}" stroke="#d6f5df" stroke-width="3" stroke-linecap="round"/></g>')


def scene_hero():
    return (sky("#0b3d2e", "#1f7a4d", "#f4b942") + sun(1190, 168, 40) + hills("#2c7a4b", 200, 14, 1, .75) + baobab(1480, 250, .5, "#1a2a1a")
            + baobab(1010, 246, .3, "#173322") + hills("#246b40", 232, 14, 2) + field(720, 1600, 3, 232, 5, .6)
            + adinkra_row(150, 120, ["sankofa", "dwennimmen"], 130, s=.85, opacity=.22) + left_shade() + kente())


def scene_inventory():
    return (sky("#14304a", "#2d6a6f", "#f2b350") + sun(1270, 150, 32, False) + hills("#1f5c46", 236, 12, 3) + warehouse(1140, 276, .62)
            + sack_pile(1450, 276, 3, .6, 2, 7) + sack_pile(800, 276, 2, .55, 2, 9) + adinkra_row(150, 120, ["nyame", "aya"], 130, s=.85, opacity=.2) + left_shade() + kente())


def scene_stock():
    return (sky("#2a1a10", "#8a4a1c", "#f7b34a") + sun(1010, 175, 30, False) + hills("#3c2a1a", 238, 10, 4)
            + warehouse(1290, 276, .56) + sack_pile(900, 276, 4, .68, 2, 11) + sack_pile(1530, 276, 2, .6, 2, 12)
            + adinkra_row(150, 120, ["aya", "akoma"], 130, s=.85, opacity=.2) + left_shade() + kente())


def scene_sales():
    return (sky("#0c3b52", "#2b8a8a", "#f7c65a") + sun(1090, 150, 34, False) + hills("#2f7d4e", 238, 12, 6)
            + umbrella(1200, 276, .86, (RED, GOLD, GREEN, GOLD, RED, GREEN)) + umbrella(1450, 276, .68, (GOLD, GREEN, RED, GREEN, GOLD))
            + sack_pile(1190, 276, 2, .52, 2, 5) + basket(1365, 276, .68) + sack(1040, 276, .56, -4) + adinkra_row(150, 120, ["akoma", "sankofa"], 130, s=.85, opacity=.22)
            + left_shade() + kente())


def scene_purchases():
    return (sky("#0d3b2a", "#3a8a4a", "#f4c152") + sun(1350, 140, 32) + hills("#3a8b50", 214, 14, 7, .8) + field(820, 1600, 3, 226, 8, .5)
            + truck(1160, 280, .86) + adinkra_row(150, 120, ["aya", "nyame"], 130, s=.85, opacity=.22) + left_shade() + kente())


def scene_customers():
    g = sky("#0a2e46", "#1f6f74", "#f6bd52") + sun(1450, 135, 30, False) + hills("#2b7a4a", 240, 12, 9)
    sets = [(RED, GOLD, GREEN), (GOLD, GREEN, RED), (GREEN, RED, GOLD), (RED, GREEN, GOLD)]
    for i, x in enumerate((900, 1090, 1280, 1470)):
        g += umbrella(x, 276, .74, sets[i] + (sets[i][1], sets[i][0]))
    g += person(960, 272, .8, RED) + person(1150, 274, .85, GREEN, "#5a3520") + person(1340, 272, .8, GOLD, "#7a4a2a") + person(1530, 274, .85, RED, "#6b4226")
    return g + adinkra_row(150, 120, ["akoma", "dwennimmen"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_payments():
    g = sky("#06321f", "#0f6b3c", "#e9b82f") + hills("#1f7a44", 246, 10, 10, .7)
    g += banknote(1290, 236, 130, 64, -8) + banknote(1330, 222, 130, 64, 4) + phone(1180, 170, .78, -8)
    g += cedi_coin(1030, 214, 36) + cedi_coin(1440, 190, 30) + cedi_coin(1500, 250, 24) + black_star(1000, 110, 18, BLACK, GOLD)
    return g + adinkra_row(150, 120, ["akoma", "sankofa"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_invoice():
    g = sky("#102f4a", "#23767c", "#f2c25c") + hills("#2d7d4d", 244, 12, 11, .8)
    g += document(1070, 168, .82, -7) + document(1250, 176, .9, 5) + document(1430, 172, .76, 10, "#f6f1df")
    g += sack(1545, 276, .4, 6) + f'<g transform="translate(930,262) rotate(-16)"><rect width="120" height="10" rx="5" fill="{GOLD}"/><polygon points="120,0 138,5 120,10" fill="{BLACK}"/></g>'
    return g + adinkra_row(150, 120, ["nyame", "akoma"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_reports():
    g = sky("#0b3b32", "#2a8a5a", "#f3bb49") + sun(1490, 120, 26) + hills("#2f8050", 232, 14, 12, .8) + field(1000, 1600, 2, 240, 13, .45)
    g += bars(900, 276, [50, 72, 62, 96, 84, 120, 138], 38, 20)
    g += f'<path d="M915,216 L990,196 L1065,206 L1145,164 L1225,178 L1305,132 L1390,110" stroke="#fff" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round" opacity=".85"/>'
    g += "".join(f'<circle cx="{x}" cy="{y}" r="7" fill="{GOLD}" stroke="#fff" stroke-width="2.5"/>' for x, y in [(990, 196), (1145, 164), (1305, 132), (1390, 110)])
    return g + adinkra_row(150, 120, ["aya", "sankofa"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_analytics():
    g = sky("#08281f", "#16644a", "#e8b43a") + sun(1350, 160, 36) + hills("#2a7549", 236, 14, 14, .8) + field(900, 1600, 2, 244, 15, .5)
    g += bars(960, 276, [44, 66, 54, 86, 74, 108, 96, 132], 36, 18) + black_star(1530, 90, 18, BLACK, GOLD)
    return g + adinkra_row(150, 120, ["dwennimmen", "akoma"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_assistant():
    g = sky("#04130c", "#0a3a25", "#1c6a43") + stars(21, 44, (0, 0, W, 190)) + hills("#0f4a2f", 240, 12, 16)
    g += f'<circle cx="1215" cy="140" r="130" fill="url(#glow)" opacity=".7"/>' + black_star(1215, 140, 62, GOLD, "#fff3b0")
    for dx, dy, r in [(-150, -40, 8), (140, -30, 7), (-96, 60, 6), (120, 66, 6), (190, 20, 4)]:
        g += f'<circle cx="{1215 + dx}" cy="{140 + dy}" r="{r}" fill="{GOLD}" opacity=".85"/>'
    g += field(900, 1600, 2, 250, 17, .5) + adinkra_row(150, 120, ["sankofa", "nyame"], 130, s=.85, opacity=.24)
    return g + left_shade() + kente()


def scene_profile():
    g = sky("#0b2f1f", "#155e3a", "#c99a25") + black_star(1250, 150, 84, BLACK, GOLD) + hills("#1b5e3a", 252, 10, 18, .8)
    g += adinkra("akoma", 1040, 110, .9, GOLD, .5) + adinkra("dwennimmen", 1500, 100, .85, GOLD, .5) + adinkra("aya", 1060, 226, .85, GOLD, .4) + adinkra("sankofa", 1470, 226, .85, GOLD, .4)
    return g + adinkra_row(150, 120, ["nyame", "akoma"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_users():
    g = sky("#0a2f24", "#17704a", "#e7b63c") + hills("#267a4b", 246, 12, 19, .75)
    tops = [RED, GREEN, GOLD, RED, GREEN, GOLD]
    skins = ["#6b4226", "#5a3520", "#7a4a2a", "#4e2e1a", "#6b4226", "#5a3520"]
    for i in range(6):
        g += person(880 + i * 120, 276 - (i % 2) * 10, .95 + (i % 2) * .1, tops[i], skins[i])
    g += f'<path d="M860,170 Q1200,100 1540,170" stroke="{GOLD}" stroke-width="4" fill="none" stroke-dasharray="2 12" stroke-linecap="round" opacity=".7"/>'
    g += black_star(1200, 92, 20, BLACK, GOLD) + adinkra_row(150, 120, ["akoma", "aya"], 130, s=.85, opacity=.22)
    return g + left_shade() + kente()


def scene_audit():
    g = sky("#051b2e", "#0f4c5c", "#1d8a6a") + stars(31, 22, (0, 0, W, 150)) + hills("#0f4f3b", 244, 12, 20)
    g += shield(1180, 172, .98) + magnifier(1400, 186, .8) + black_star(1000, 88, 16, GOLD, "#fff3b0")
    return g + adinkra_row(150, 120, ["nyame", "dwennimmen"], 130, s=.85, opacity=.22) + left_shade() + kente()


def scene_deleted():
    g = sky("#2b1b10", "#7a4a25", "#e9a93d") + hills("#46301c", 246, 12, 22, .8)
    g += basket(1180, 276, 1.0) + basket(1370, 276, .8) + sack(1520, 276, .6, 4) + sack(1010, 276, .56, -5) + restore_arrow(1180, 128, 42)
    return g + adinkra_row(150, 120, ["sankofa", "aya"], 130, s=.85, opacity=.24) + left_shade() + kente()


SCENES = {
    "hero": scene_hero, "inventory": scene_inventory, "stock": scene_stock, "sales": scene_sales, "purchases": scene_purchases,
    "customers": scene_customers, "payments": scene_payments, "invoice": scene_invoice, "reports": scene_reports,
    "assistant": scene_assistant, "profile": scene_profile, "users": scene_users, "audit_logs": scene_audit,
    "deleted_records": scene_deleted, "analytics": scene_analytics,
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, scene in SCENES.items():
        svg = frame(scene(), name.replace("_", " "))
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
        print(f"{name:16} {len(svg) / 1024:6.1f} KB")


if __name__ == "__main__":
    main()
