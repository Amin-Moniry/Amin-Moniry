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
# One design system for every card: 800 px wide, 8 px inner frame, 40 px side
# padding, minimum text size 15 (caps) / 19 (italic) so it stays legible when
# GitHub scales the image down on a phone.
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
<linearGradient id="plate" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2a0a4a"/><stop offset="1" stop-color="#16041f"/></linearGradient>
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
    total = sum(hmtx[cmap.get(ord(ch), ".notdef")][0] if cmap.get(ord(ch)) else upm * 0.5 for ch in s)
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


KEY_PATTERN = ('<pattern id="key" x="0" y="0" width="20" height="20" patternUnits="userSpaceOnUse">'
               '<path d="M0 17H20M3 17V3H17V14H8V8H12" fill="none" stroke="#D4AF37" stroke-width="1.2"/></pattern>\n')


class Card:
    def __init__(self, w, h, name="card"):
        self.w, self.h, self.name = w, h, name
        self.parts, self.chars, self.boxes, self.extra_defs = [], set(), [], ""

    def add(self, s):
        self.parts.append(s)

    def text(self, cls, x, y, size, fill, s, anchor="start", spacing=0):
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
        self.add(f'<text class="{cls}" x="{ax:g}" y="{y:g}" font-size="{size:g}"{sp} text-anchor="{anchor}" '
                 f'fill="{fill}">{escape(s)}</text>')

    def frame(self):
        w, h = self.w, self.h
        self.add(f'<rect x="1" y="1" width="{w-2}" height="{h-2}" rx="6" fill="url(#plate)" stroke="url(#gold)" stroke-width="1.6"/>')
        self.add(f'<rect x="8" y="8" width="{w-16}" height="{h-16}" rx="3" fill="none" stroke="#D4AF37" stroke-opacity=".35"/>')

    def title_zone(self, title, note):
        self.text("cz", 40, 44, 17, "url(#gold)", title, "start", 6)
        if note:
            self.text("it", self.w - 40, 44, 20, "#cbb8ec", note, "end")
        self.add(f'<line x1="40" y1="60" x2="{self.w-40}" y2="60" stroke="url(#fadeh)" stroke-width="1.2"/>')

    def medallion(self, cx, cy, r, label, size=20, spin=1):
        self.add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#1c0533" stroke="url(#gold)" stroke-width="2.2"/>'
                 f'<circle cx="{cx}" cy="{cy}" r="{r-5}" fill="none" stroke="#D4AF37" stroke-opacity=".45"/>'
                 f'<circle cx="{cx}" cy="{cy}" r="{r+7}" fill="none" stroke="#F6E7A8" stroke-opacity=".5" stroke-dasharray="2 6">'
                 f'<animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="{360*spin} {cx} {cy}" '
                 f'dur="36s" repeatCount="indefinite"/></circle>')
        if label:
            self.text("cz", cx, cy + size * 0.36, size, "url(#gold)", label, "middle")
        else:
            self.add(f'<rect x="{cx-5}" y="{cy-5}" width="10" height="10" fill="url(#goldv)" transform="rotate(45 {cx} {cy})"/>')

    def check(self):
        """Report text that overlaps other text or crosses the frame padding."""
        bad = []
        for i, a in enumerate(self.boxes):
            if a[0] < 14 or a[2] > self.w - 14 or a[1] < 14 or a[3] > self.h - 14:
                bad.append(f"{self.name}: '{a[4]}' leaves the safe area")
            for b in self.boxes[i + 1:]:
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    bad.append(f"{self.name}: '{a[4]}' overlaps '{b[4]}'")
        for m in bad:
            print("  WARN", m, file=sys.stderr)
        return not bad

    def svg(self):
        self.check()
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
                f'xmlns="http://www.w3.org/2000/svg" role="img">\n<defs>\n{DEFS}{self.extra_defs}</defs>\n'
                f'{style(self.chars)}\n' + "\n".join(self.parts) + "\n</svg>\n")


# ----------------------------------------------------------------- cards ---
NUM_Y, LABEL_Y, NOTE_Y, CARD_H = 117, 162, 188, 214      # shared by stats + streak


def column(c, cx, num, label, note):
    c.text("cz", cx, NUM_Y, 46, "url(#gold)", num, "middle")
    c.text("cm", cx, LABEL_Y, 15, "#EDE6D6", label, "middle", 3)
    c.text("it", cx, NOTE_Y, 19, "#9d86c9", note, "middle")


def card_stats(d, total):
    c = Card(W, CARD_H, "stats")
    c.frame()
    c.title_zone("STATISTICA", f"@{d['login']}")
    cols = [W / 6, W / 2, W * 5 / 6]
    column(c, cols[0], f"{d['repos']:,}", "REPOSITORIES", "own and public")
    column(c, cols[1], f"{d['stars']:,}", "STARS EARNED", "across all repositories")
    column(c, cols[2], f"{d['followers']:,}", "FOLLOWERS", "on GitHub")
    for x in (W / 3, W * 2 / 3):
        c.add(f'<line x1="{x:.1f}" y1="82" x2="{x:.1f}" y2="192" stroke="url(#fadev)" stroke-width="1.2"/>')
    return c.svg()


BAR_FILLS = ["url(#gold)", "url(#vio)", "#C8B8FF", "#D4AF37", "#7B2FFF", "#9d86c9"]


def card_langs(d):
    langs = sorted(d["langs"].items(), key=lambda kv: -kv[1])[:6]
    total = sum(d["langs"].values()) or 1
    n = max(len(langs), 1)
    pitch = 46
    c = Card(W, 76 + n * pitch + 22, "langs")
    c.frame()
    c.title_zone("LINGUAE", "most used languages, " + d["basis"])
    if not langs:
        c.text("it", W / 2, 120, 20, "#cbb8ec", "no language data yet", "middle")
        return c.svg()
    x0, x1 = 40, W - 40
    for i, (name, v) in enumerate(langs):
        pct = v * 100 / total
        y = 98 + i * pitch
        c.text("cm", x0, y, 15, "#EDE6D6", name.upper(), "start", 2.5)
        c.text("cz", x1, y, 15, "url(#gold)", f"{pct:.1f}%", "end")
        wfull = max(8.0, (x1 - x0) * pct / 100)
        c.add(f'<rect x="{x0}" y="{y+11}" width="{x1-x0}" height="8" rx="4" fill="#12031f" stroke="#D4AF37" stroke-opacity=".3"/>')
        c.add(f'<rect x="{x0}" y="{y+11}" width="{wfull:.1f}" height="8" rx="4" fill="{BAR_FILLS[i % len(BAR_FILLS)]}">'
              f'<animate attributeName="width" from="0" to="{wfull:.1f}" dur="{0.9 + i*0.12:.2f}s" begin="0s" fill="freeze" '
              f'calcMode="spline" keyTimes="0;1" keySplines=".2 .7 .2 1"/></rect>')
    return c.svg()


def card_streak(d, total):
    cur, cur_r, best, best_r = streaks(d["days"])
    c = Card(W, CARD_H, "streak")
    c.frame()
    c.title_zone("SERIES", "daily contributions")
    cols = [W / 6, W / 2, W * 5 / 6]
    column(c, cols[0], f"{total:,}", "CONTRIBUTIONS", "last twelve months")
    column(c, cols[2], str(best), "LONGEST STREAK", fmt_range(best_r))
    # centre column: the current streak sits inside a ring
    cx, cy = cols[1], 103
    c.add(f'<circle cx="{cx:g}" cy="{cy}" r="31" fill="#1c0533" stroke="url(#gold)" stroke-width="2.6"/>'
          f'<circle cx="{cx:g}" cy="{cy}" r="25" fill="none" stroke="#D4AF37" stroke-opacity=".4"/>'
          f'<circle cx="{cx:g}" cy="{cy}" r="39" fill="none" stroke="#F6E7A8" stroke-opacity=".5" stroke-dasharray="2 6">'
          f'<animateTransform attributeName="transform" type="rotate" from="0 {cx:g} {cy}" to="360 {cx:g} {cy}" dur="30s" repeatCount="indefinite"/></circle>')
    c.text("cz", cx, cy + 14, 38, "url(#gold)", str(cur), "middle")
    c.text("cm", cx, LABEL_Y, 15, "#EDE6D6", "CURRENT STREAK", "middle", 3)
    c.text("it", cx, NOTE_Y, 19, "#9d86c9", fmt_range(cur_r), "middle")
    for x in (W / 3, W * 2 / 3):
        c.add(f'<line x1="{x:.1f}" y1="82" x2="{x:.1f}" y2="192" stroke="url(#fadev)" stroke-width="1.2"/>')
    return c.svg()


HEAT = ["#26093f", "#4B1F86", "#7B2FFF", "#C9A23C", "#F6E7A8"]


def card_activity(d, total):
    days = d["days"]
    c = Card(W, 256, "activity")
    c.frame()
    c.title_zone("ACTA DIURNA", f"{total:,} contributions in the last year")
    if not days:
        return c.svg()
    first = dt.date.fromisoformat(days[0]["date"])
    origin = first - dt.timedelta(days=(first.weekday() + 1) % 7)       # Sunday on/before first day
    x0, y0, pitch, cell = 76, 104, 12.9, 10.2
    last_col, last_month = -9, None
    for dd in days:
        date = dt.date.fromisoformat(dd["date"])
        col, row = (date - origin).days // 7, (date.weekday() + 1) % 7
        x, y = x0 + col * pitch, y0 + row * pitch
        c.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell}" height="{cell}" rx="2.4" fill="{HEAT[min(dd["level"], 4)]}"/>')
        if date.day <= 7 and row == 0 and date.month != last_month and col - last_col >= 4:
            c.text("cm", round(x, 1), 94, 13, "#9d86c9", date.strftime("%b").upper(), "start", 1.5)
            last_col, last_month = col, date.month
    for r, lab in ((1, "MON"), (3, "WED"), (5, "FRI")):
        c.text("cm", 66, round(y0 + r * pitch + 9.5, 1), 12, "#9d86c9", lab, "end", 1)
    ly = 226
    lx = W - 40 - 62 - 5 * 18
    c.text("it", lx - 8, ly + 9, 19, "#9d86c9", "less", "end")
    for i, col in enumerate(HEAT):
        c.add(f'<rect x="{lx + i*18}" y="{ly}" width="12" height="12" rx="2.4" fill="{col}"/>')
    c.text("it", lx + 5 * 18 + 6, ly + 9, 19, "#9d86c9", "more", "start")
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
