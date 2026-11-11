#!/usr/bin/env python3
"""Generates the four ACTA cards (stats, langs, streak, activity) as local SVGs.

Nothing here depends on a third-party image service: the cards are written to
assets/ and committed to the repo, so they always load.

Data source:
  * with GITHUB_TOKEN  -> GitHub REST + GraphQL API (language sizes by bytes)
  * without / on error -> public github.com pages (languages by repository)
"""
import base64, datetime as dt, io, json, os, re, sys, urllib.error, urllib.request
from collections import Counter
from html import escape, unescape
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

USER = os.environ.get("GH_USER", "Amin-Moniry")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONT_DIR = Path(__file__).resolve().parent / "fonts"


# ----------------------------------------------------------------- http ----
def http(url, data=None, headers=None, text=False):
    h = {"User-Agent": "profile-stats-script", **(headers or {})}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf8", "replace")
    return raw if text else json.loads(raw)


def api(path, data=None):
    return http("https://api.github.com" + path, data,
                {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})


LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
          "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}


# ------------------------------------------------------------ data: API ----
def fetch_api():
    user = api(f"/users/{USER}")
    repos, page = [], 1
    while True:
        chunk = api(f"/users/{USER}/repos?per_page=100&type=owner&page={page}")
        repos += chunk
        if len(chunk) < 100:
            break
        page += 1
    own = [r for r in repos if not r["fork"]]
    langs = Counter()
    for r in own[:80]:
        try:
            for k, v in api(f"/repos/{r['full_name']}/languages").items():
                langs[k] += v
        except Exception:
            pass
    q = ("query($u:String!){user(login:$u){contributionsCollection{contributionCalendar{"
         "totalContributions weeks{contributionDays{date contributionCount contributionLevel}}}}}}")
    g = api("/graphql", {"query": q, "variables": {"u": USER}})
    cal = g["data"]["user"]["contributionsCollection"]["contributionCalendar"]
    days = [{"date": d["date"], "count": d["contributionCount"], "level": LEVELS.get(d["contributionLevel"], 0)}
            for w in cal["weeks"] for d in w["contributionDays"]]
    return {"login": user["login"], "followers": user["followers"], "repos": len(own),
            "stars": sum(r["stargazers_count"] for r in own), "langs": dict(langs),
            "basis": "by code size", "days": days}


# ------------------------------------------------------------ data: HTML ---
def to_int(s):
    s = s.strip().lower().replace(",", "")
    return int(float(s[:-1]) * 1000) if s.endswith("k") else int(float(s))


def fetch_html():
    base = f"https://github.com/{USER}"
    page = http(base, text=True)
    m = re.search(r'tab=followers"[^>]*>(.*?)</a>', page, re.S)
    followers = to_int(re.search(r"([\d.,]+k?)", re.sub(r"<[^>]+>", " ", m.group(1))).group(1)) if m else 0

    cal = http(f"https://github.com/users/{USER}/contributions", text=True)
    tips = {i: t for i, t in re.findall(r'<tool-tip[^>]*?for="([^"]+)"[^>]*>([^<]*)</tool-tip>', cal)}
    days = []
    for tag in re.findall(r"<td[^>]*ContributionCalendar-day[^>]*>", cal):
        date = re.search(r'data-date="([\d-]+)"', tag)
        if not date:
            continue
        ident = re.search(r'id="([^"]+)"', tag)
        level = re.search(r'data-level="(\d)"', tag)
        tip = tips.get(ident.group(1), "") if ident else ""
        n = re.match(r"\s*([\d,]+) contribution", tip)
        days.append({"date": date.group(1), "count": int(n.group(1).replace(",", "")) if n else 0,
                     "level": int(level.group(1)) if level else 0})
    days.sort(key=lambda d: d["date"])

    repos, langs, stars = 0, Counter(), 0
    for p in range(1, 8):
        html = http(f"{base}?tab=repositories&type=source&page={p}", text=True)
        items = html.split('itemprop="owns"')[1:]
        if not items:
            break
        for it in items:
            if "Forked from" in it:
                continue
            repos += 1
            lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', it)
            if lang:
                langs[unescape(lang.group(1)).strip()] += 1
            st = re.search(r'/stargazers"[^>]*>(.*?)</a>', it, re.S)
            if st:
                v = re.sub(r"<[^>]+>|\s+", "", st.group(1))
                stars += to_int(v) if re.fullmatch(r"[\d.,]+k?", v.lower()) else 0
        if len(items) < 30:
            break
    return {"login": USER, "followers": followers, "repos": repos, "stars": stars,
            "langs": dict(langs), "basis": "by repository", "days": days}


# ---------------------------------------------------------- derived data ---
def streaks(days):
    cur_end = len(days) - 1
    if cur_end >= 0 and days[cur_end]["count"] == 0:
        cur_end -= 1                      # today may simply not have a contribution yet
    cur, i = 0, cur_end
    while i >= 0 and days[i]["count"] > 0:
        cur += 1
        i -= 1
    cur_range = (days[i + 1]["date"], days[cur_end]["date"]) if cur else None
    best, run, start, best_range = 0, 0, 0, None
    for j, d in enumerate(days):
        if d["count"] > 0:
            if run == 0:
                start = j
            run += 1
            if run > best:
                best, best_range = run, (days[start]["date"], d["date"])
        else:
            run = 0
    return cur, cur_range, best, best_range


def fmt_date(s):
    d = dt.date.fromisoformat(s)
    return f"{d.strftime('%b')} {d.day}"


def fmt_range(r):
    if not r:
        return "no streak yet"
    a, b = r
    return fmt_date(a) if a == b else f"{fmt_date(a)} - {fmt_date(b)}"


# --------------------------------------------------------------- svg kit ---
# One design system for every card: 800 px wide, hairline frames, compact
# heights and light typography so the profile reads as refined, not heavy.
W = 800

DEFS = """<linearGradient id="gold" x1="0" y1="0" x2="1" y2="0.3">
 <stop offset="0" stop-color="#9c7a22"/><stop offset=".3" stop-color="#F6E7A8"/>
 <stop offset=".6" stop-color="#D4AF37"/><stop offset="1" stop-color="#8f6f1c"/>
</linearGradient>
<linearGradient id="goldv" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stop-color="#F6E7A8"/><stop offset=".5" stop-color="#D4AF37"/><stop offset="1" stop-color="#8f6f1c"/>
</linearGradient>
<linearGradient id="shw" x1="0" x2="1">
 <stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".95"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
</linearGradient>
<linearGradient id="fadeh" x1="0" x2="1">
 <stop offset="0" stop-color="#D4AF37" stop-opacity="0"/><stop offset=".5" stop-color="#D4AF37"/><stop offset="1" stop-color="#D4AF37" stop-opacity="0"/>
</linearGradient>
<linearGradient id="fadev" x1="0" y1="0" x2="0" y2="1">
 <stop offset="0" stop-color="#D4AF37" stop-opacity="0"/><stop offset=".5" stop-color="#D4AF37"/><stop offset="1" stop-color="#D4AF37" stop-opacity="0"/>
</linearGradient>
<linearGradient id="plate" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#220a3d"/><stop offset="1" stop-color="#14041d"/></linearGradient>
<linearGradient id="vio" x1="0" x2="1"><stop offset="0" stop-color="#4B00D1"/><stop offset="1" stop-color="#7B2FFF"/></linearGradient>
<radialGradient id="glow"><stop offset="0" stop-color="#F6E7A8" stop-opacity=".9"/><stop offset=".4" stop-color="#D4AF37" stop-opacity=".35"/><stop offset="1" stop-color="#D4AF37" stop-opacity="0"/></radialGradient>
"""

FONT_FILES = {"cz": "cinzel-latin-700-normal.woff2", "cm": "cinzel-latin-500-normal.woff2",
              "it": "cormorant-garamond-latin-500-italic.woff2"}
_metrics = {}


def advance(cls, s, size, spacing=0):
    """Approximate rendered width of s (font advances + letter-spacing)."""
    if cls not in _metrics:
        f = TTFont(str(FONT_DIR / FONT_FILES[cls]))
        _metrics[cls] = (f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm)
    cmap, hmtx, upm = _metrics[cls]
    total = sum(hmtx[cmap[ord(ch)]][0] if ord(ch) in cmap else upm * 0.5 for ch in s)
    return total * size / upm + spacing * len(s)


def subset_b64(path, chars):
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern", "liga"]
    f = TTFont(str(path))
    s = subset.Subsetter(opts)
    s.populate(text="".join(sorted(chars)) + " ")
    s.subset(f)
    buf = io.BytesIO()
    f.flavor = "woff2"
    f.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def style(chars):
    spec = [("Cinzel", 700, "normal", "cz"), ("Cinzel", 500, "normal", "cm"),
            ("Cormorant Garamond", 500, "italic", "it")]
    css = "".join(
        f"@font-face{{font-family:'{fam}';font-weight:{w};font-style:{st};"
        f"src:url(data:font/woff2;base64,{subset_b64(FONT_DIR / FONT_FILES[k], chars)}) format('woff2')}}"
        for fam, w, st, k in spec)
    css += ("\n.cz{font-family:'Cinzel','Trajan Pro','Palatino Linotype',Georgia,serif;font-weight:700}"
            "\n.cm{font-family:'Cinzel','Trajan Pro','Palatino Linotype',Georgia,serif;font-weight:500}"
            "\n.it{font-family:'Cormorant Garamond','Palatino Linotype',Georgia,serif;font-style:italic;font-weight:500}")
    return f"<style>{css}\n</style>"


class Card:
    def __init__(self, w, h, name="card", margin=10):
        self.w, self.h, self.name, self.margin = w, h, name, margin
        self.parts, self.chars, self.boxes, self.extra_defs = [], set(), [], ""

    def add(self, s):
        self.parts.append(s)

    def text(self, cls, x, y, size, fill, s, anchor="start", spacing=0, reveal=None, sheen=None):
        """reveal=(fraction, seconds): fade in once on load.  sheen=(seconds, begin): looping highlight sweep."""
        self.chars |= set(s)
        wd = advance(cls, s, size, spacing)
        ax = x
        if anchor == "middle":
            ax = x + spacing / 2
            x0 = x - wd / 2
        elif anchor == "end":
            ax = x + spacing
            x0 = x - wd
        else:
            x0 = x
        self.boxes.append((x0, y - size * 0.74, x0 + wd, y + size * 0.24, s))
        sp = f' letter-spacing="{spacing}"' if spacing else ""
        anim = ""
        if reveal:
            f, d = reveal
            anim = (f'<animate attributeName="opacity" values="0;0;1" keyTimes="0;{f:.3f};1" dur="{d}s" '
                    f'begin="0s" fill="freeze"/>')
        self.add(f'<text class="{cls}" x="{ax:g}" y="{y:g}" font-size="{size:g}"{sp} text-anchor="{anchor}" '
                 f'fill="{fill}">{escape(s)}{anim}</text>')
        if sheen:
            dur, begin = sheen
            mid = f"sh{len(self.parts)}"
            self.extra_defs += (f'<mask id="{mid}"><text class="{cls}" x="{ax:g}" y="{y:g}" font-size="{size:g}"{sp} '
                                f'text-anchor="{anchor}" fill="#fff">{escape(s)}</text></mask>\n')
            xs, xe = x0 - 50, x0 + wd + 10
            self.add(f'<g mask="url(#{mid})"><rect x="{xs:.0f}" y="{y-size:g}" width="44" height="{size*1.5:g}" '
                     f'fill="url(#shw)" opacity=".85"><animate attributeName="x" values="{xs:.0f};{xs:.0f};{xe:.0f}" '
                     f'keyTimes="0;.4;1" dur="{dur}s" begin="{begin}s" repeatCount="indefinite"/></rect></g>')

    def frame(self, x=0, y=0, w=None, h=None):
        w = self.w if w is None else w
        h = self.h if h is None else h
        self.add(f'<rect x="{x+.5}" y="{y+.5}" width="{w-1}" height="{h-1}" rx="3" fill="url(#plate)" stroke="url(#gold)" stroke-width="1"/>'
                 f'<rect x="{x+5}" y="{y+5}" width="{w-10}" height="{h-10}" rx="1.5" fill="none" stroke="#D4AF37" stroke-opacity=".2" stroke-width=".8"/>')

    def meander(self, x, y, n, h=9, opacity=.7):
        """Greek key: n cells, each h x h, drawn as one hairline path."""
        k, d = h / 20, []
        for i in range(n):
            ox = x + i * h
            d.append(f"M{ox:g} {y+17*k:g}H{ox+h:g}M{ox+3*k:g} {y+17*k:g}V{y+3*k:g}H{ox+17*k:g}V{y+14*k:g}H{ox+8*k:g}V{y+8*k:g}H{ox+12*k:g}")
        self.add(f'<path d="{"".join(d)}" fill="none" stroke="#D4AF37" stroke-width=".9" stroke-opacity="{opacity}"/>')

    def diamond(self, cx, cy, r=3.2):
        self.add(f'<rect x="{cx-r}" y="{cy-r}" width="{2*r}" height="{2*r}" fill="url(#goldv)" transform="rotate(45 {cx} {cy})"/>')

    def title_zone(self, title, note):
        self.text("cz", 28, 31, 13, "url(#gold)", title, "start", 5)
        if note:
            self.text("it", self.w - 28, 31, 17, "#cbb8ec", note, "end")
        self.add(f'<line x1="28" y1="42" x2="{self.w-28}" y2="42" stroke="url(#fadeh)" stroke-width=".9"/>')

    def medallion(self, cx, cy, r, label, size=14, spin=1):
        self.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#1c0533" stroke="url(#gold)" stroke-width="1.3"/>'
                 f'<circle cx="{cx}" cy="{cy}" r="{r-3.5}" fill="none" stroke="#D4AF37" stroke-opacity=".35" stroke-width=".8"/>'
                 f'<circle cx="{cx}" cy="{cy}" r="{r+5}" fill="none" stroke="#F6E7A8" stroke-opacity=".45" stroke-width=".8" stroke-dasharray="1.5 5">'
                 f'<animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="{360*spin} {cx} {cy}" '
                 f'dur="14s" repeatCount="indefinite"/></circle>')
        if label:
            self.text("cz", cx, cy + size * 0.36, size, "url(#gold)", label, "middle")
        else:
            self.diamond(cx, cy, 3.4)

    def check(self):
        """Report text that overlaps other text or crosses the frame padding."""
        m, bad = self.margin, []
        for i, a in enumerate(self.boxes):
            if a[0] < m or a[2] > self.w - m or a[1] < m or a[3] > self.h - m:
                bad.append(f"{self.name}: '{a[4]}' leaves the safe area")
            for b in self.boxes[i + 1:]:
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    bad.append(f"{self.name}: '{a[4]}' overlaps '{b[4]}'")
        for msg in bad:
            print("  WARN", msg, file=sys.stderr)
        return not bad

    def svg(self):
        self.check()
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
                f'xmlns="http://www.w3.org/2000/svg" role="img">\n<defs>\n{DEFS}{self.extra_defs}</defs>\n'
                f'{style(self.chars)}\n' + "\n".join(self.parts) + "\n</svg>\n")


# ----------------------------------------------------------------- cards ---
NUM_Y, LABEL_Y, NOTE_Y, CARD_H = 92, 117, 138, 156      # shared by stats + streak


def column(c, cx, num, label, note, k=0):
    f = 0.08 + 0.22 * k
    c.text("cz", cx, NUM_Y, 32, "url(#gold)", num, "middle", reveal=(f, 1.0), sheen=(3.6, 0.6 * k + 1.0))
    c.text("cm", cx, LABEL_Y, 12.5, "#EDE6D6", label, "middle", 3, reveal=(f + 0.1, 1.0))
    c.text("it", cx, NOTE_Y, 17, "#9d86c9", note, "middle", reveal=(f + 0.2, 1.0))


def dividers(c):
    for x in (W / 3, W * 2 / 3):
        c.add(f'<line x1="{x:.1f}" y1="60" x2="{x:.1f}" y2="140" stroke="url(#fadev)" stroke-width=".9"/>')


def card_stats(d, total):
    c = Card(W, CARD_H, "stats")
    c.frame()
    c.title_zone("STATISTICA", f"@{d['login']}")
    cols = [W / 6, W / 2, W * 5 / 6]
    column(c, cols[0], f"{d['repos']:,}", "REPOSITORIES", "own and public", 0)
    column(c, cols[1], f"{d['stars']:,}", "STARS EARNED", "across all repositories", 1)
    column(c, cols[2], f"{d['followers']:,}", "FOLLOWERS", "on GitHub", 2)
    dividers(c)
    return c.svg()


BAR_FILLS = ["url(#gold)", "url(#vio)", "#C8B8FF", "#D4AF37", "#7B2FFF", "#9d86c9"]


def card_langs(d):
    langs = sorted(d["langs"].items(), key=lambda kv: -kv[1])[:6]
    total = sum(d["langs"].values()) or 1
    n = max(len(langs), 1)
    pitch = 34
    c = Card(W, 62 + n * pitch + 12, "langs")
    c.frame()
    c.title_zone("LINGUAE", "most used languages, " + d["basis"])
    if not langs:
        c.text("it", W / 2, 88, 18, "#cbb8ec", "no language data yet", "middle")
        return c.svg()
    x0, x1 = 28, W - 28
    for i, (name, v) in enumerate(langs):
        pct = v * 100 / total
        y = 72 + i * pitch
        c.text("cm", x0, y, 12.5, "#EDE6D6", name.upper(), "start", 2.5)
        c.text("cz", x1, y, 12.5, "url(#gold)", f"{pct:.1f}%", "end")
        wfull = max(6.0, (x1 - x0) * pct / 100)
        c.add(f'<rect x="{x0}" y="{y+8}" width="{x1-x0}" height="4" rx="2" fill="#12031f" stroke="#D4AF37" stroke-opacity=".25" stroke-width=".6"/>')
        c.add(f'<rect x="{x0}" y="{y+8}" width="{wfull:.1f}" height="4" rx="2" fill="{BAR_FILLS[i % len(BAR_FILLS)]}">'
              f'<animate attributeName="width" from="0" to="{wfull:.1f}" dur="{0.55 + i*0.08:.2f}s" begin="0s" fill="freeze" '
              f'calcMode="spline" keyTimes="0;1" keySplines=".2 .7 .2 1"/></rect>')
    return c.svg()


def card_streak(d, total):
    cur, cur_r, best, best_r = streaks(d["days"])
    c = Card(W, CARD_H, "streak")
    c.frame()
    c.title_zone("SERIES", "daily contributions")
    cols = [W / 6, W / 2, W * 5 / 6]
    column(c, cols[0], f"{total:,}", "CONTRIBUTIONS", "last twelve months", 0)
    column(c, cols[2], str(best), "LONGEST STREAK", fmt_range(best_r), 2)
    cx, cy = cols[1], 80                      # the current streak sits inside a hairline ring
    c.add(f'<circle cx="{cx:g}" cy="{cy}" r="21" fill="#1c0533" stroke="url(#gold)" stroke-width="1.4"/>'
          f'<circle cx="{cx:g}" cy="{cy}" r="16.5" fill="none" stroke="#D4AF37" stroke-opacity=".35" stroke-width=".8"/>'
          f'<circle cx="{cx:g}" cy="{cy}" r="27" fill="none" stroke="#F6E7A8" stroke-opacity=".45" stroke-width=".8" stroke-dasharray="1.5 5">'
          f'<animateTransform attributeName="transform" type="rotate" from="0 {cx:g} {cy}" to="360 {cx:g} {cy}" dur="10s" repeatCount="indefinite"/></circle>')
    c.text("cz", cx, cy + 9, 25, "url(#gold)", str(cur), "middle", reveal=(0.3, 1.0))
    c.text("cm", cx, LABEL_Y, 12.5, "#EDE6D6", "CURRENT STREAK", "middle", 3, reveal=(0.4, 1.0))
    c.text("it", cx, NOTE_Y, 17, "#9d86c9", fmt_range(cur_r), "middle", reveal=(0.5, 1.0))
    dividers(c)
    return c.svg()


HEAT = ["#26093f", "#4B1F86", "#7B2FFF", "#C9A23C", "#F6E7A8"]


def card_activity(d, total):
    days = d["days"]
    c = Card(W, 206, "activity")
    c.frame()
    c.title_zone("ACTA DIURNA", f"{total:,} contributions in the last year")
    if not days:
        return c.svg()
    first = dt.date.fromisoformat(days[0]["date"])
    origin = first - dt.timedelta(days=(first.weekday() + 1) % 7)       # Sunday on/before first day
    x0, y0, pitch, cell = 68, 72, 12.9, 9.6
    last_col, last_month, by_col = -9, None, {}
    for dd in days:
        date = dt.date.fromisoformat(dd["date"])
        col, row = (date - origin).days // 7, (date.weekday() + 1) % 7
        x, y = x0 + col * pitch, y0 + row * pitch
        lvl = min(dd["level"], 4)
        twinkle = ('<animate attributeName="opacity" values=".55;1;.55" dur="1.8s" repeatCount="indefinite"/>' if lvl >= 3 else "")
        by_col.setdefault(col, []).append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell}" height="{cell}" rx="2" fill="{HEAT[lvl]}">{twinkle}</rect>')
        if date.day <= 7 and row == 0 and date.month != last_month and col - last_col >= 4:
            c.text("cm", round(x, 1), 64, 11, "#9d86c9", date.strftime("%b").upper(), "start", 1.5)
            last_col, last_month = col, date.month
    ncol = max(by_col) + 1
    for col, rects in sorted(by_col.items()):                      # left-to-right wave on load
        f = 0.6 * col / ncol
        c.add(f'<g><animate attributeName="opacity" values="0;0;1" keyTimes="0;{f:.3f};1" dur="1.1s" begin="0s" fill="freeze"/>'
              + "".join(rects) + "</g>")
    for r, lab in ((1, "MON"), (3, "WED"), (5, "FRI")):
        c.text("cm", 58, round(y0 + r * pitch + 8.5, 1), 10, "#9d86c9", lab, "end", 1)
    ly = 176
    lx = W - 28 - 56 - 5 * 16
    c.text("it", lx - 7, ly + 9, 16, "#9d86c9", "less", "end")
    for i, col in enumerate(HEAT):
        c.add(f'<rect x="{lx + i*16}" y="{ly}" width="11" height="11" rx="2" fill="{col}"/>')
    c.text("it", lx + 5 * 16 + 4, ly + 9, 16, "#9d86c9", "more", "start")
    return c.svg()


# ------------------------------------------------------------------ main ---
def main():
    data = None
    if TOKEN:
        try:
            data = fetch_api()
            print("source: GitHub API")
        except Exception as e:                       # fall back to public pages
            print(f"API failed ({e}); falling back to public pages", file=sys.stderr)
    if data is None:
        data = fetch_html()
        print("source: public pages")
    if not data["days"]:
        sys.exit("no contribution data found - leaving the existing cards untouched")
    total = sum(x["count"] for x in data["days"])
    ASSETS.mkdir(exist_ok=True)
    for name, svg in (("stats.svg", card_stats(data, total)), ("langs.svg", card_langs(data)),
                      ("streak.svg", card_streak(data, total)), ("activity.svg", card_activity(data, total))):
        (ASSETS / name).write_text(svg, encoding="utf8")
        print(f"wrote assets/{name} ({len(svg)/1024:.1f} KB)")
    print({k: v for k, v in data.items() if k != "days"}, "total", total)


if __name__ == "__main__":
    main()
