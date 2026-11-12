#!/usr/bin/env python3
"""Build the profile README cards as self-contained SVGs.

Reads public data from the GitHub GraphQL API and writes SVGs to
assets/cards/. No third-party packages: stdlib only (urllib, zlib).

The brand fonts (Anton, Bebas Neue, Montserrat, Schoolbell) live in
scripts/fonts/. Each SVG embeds only the glyphs it actually uses, as a
tiny WOFF in a data URI, because an SVG rendered through <img> on
GitHub cannot load external fonts.

Usage:
    GH_TOKEN=... python -I scripts/build_cards.py
    python -I scripts/build_cards.py --from-json data.json   # offline
    python -I scripts/build_cards.py --dump-json data.json   # save API data

Token: GH_TOKEN or GITHUB_TOKEN. Only public data is read, so the
Actions GITHUB_TOKEN is enough.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "scripts" / "fonts"
OUT_DIR = ROOT / "assets" / "cards"
LOGIN = "leozitogs"

# ---------------------------------------------------------------- brand
BLACK = "#000000"
GRAY = "#222222"
GREEN = "#19BC00"
PASTEL = "#C5F49D"
OFFWHITE = "#FBFBFB"
MUTED = "#8C8C8C"  # offwhite at low intensity, for qualifiers
DIM = "#5A5A5A"
CELL = "#141414"

# Featured projects: the copy is curated here, the numbers come from the API.
PROJECTS = [
    {
        "repo": "ai-harness-compiler",
        "desc": "Compiles project intent into typed, verifiable AI engineering "
        "harnesses: context, prompts, skills, tools, guardrails and evals.",
        "tag": "OPEN SOURCE · APACHE-2.0",
    },
    {
        "repo": "vid2llm",
        "desc": "Turns any video into LLM-ready frames, with smart sampling, "
        "scene detection and OCR. Published on PyPI as vid2llm.",
        "tag": "PYPI · APACHE-2.0",
    },
    {
        "repo": "agentic-lab",
        "desc": "An open lab for AI engineering: 8 weeks of fundamentals, "
        "then 11 modules from agents and RAG to evals and LLMOps.",
        "tag": "PLAYBOOK",
        "lang_fallback": "Markdown",
    },
    {
        "repo": "leovox-studios",
        "desc": "The Leovox Studios portfolio, built as a single-take film: "
        "one continuous scene from the first scroll to contact.",
        "tag": "PORTFOLIO · IN PROGRESS",
    },
]

ARSENAL = [
    ("AI ENGINEERING", "the base", ["Claude Agent SDK", "LangGraph", "MCP", "OpenAI", "Gemini", "Ollama"]),
    ("AGENTIC CODING & HARNESS", "the edge", ["Claude Code", "Codex CLI", "OpenSpec", "GitHub Actions"]),
    ("ML & SPECIALIST MODELS", None, ["faster-whisper", "LightGBM", "scikit-learn"]),
    ("BACKEND", None, ["Python", "FastAPI", "PostgreSQL", "Node.js"]),
    ("FRONTEND", None, ["TypeScript", "React", "Next.js", "Tailwind", "GSAP", "Three.js"]),
    ("DESIGN", None, ["Figma", "Blender", "Adobe"]),
]

LINKS = [
    ("linkedin", "LINKEDIN"),
    ("behance", "BEHANCE"),
    ("instagram", "INSTAGRAM"),
    ("email", "EMAIL"),
]

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


# ============================================================ font subsetting
def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _i16(b, o):
    return struct.unpack_from(">h", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _checksum(data: bytes) -> int:
    data = data + b"\0" * (-len(data) % 4)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


class Font:
    """Minimal TrueType reader and glyph subsetter (glyf fonts only)."""

    KEEP = ("OS/2", "cmap", "glyf", "head", "hhea", "hmtx", "loca", "maxp", "name", "post")

    def __init__(self, path: Path, family: str, fallback: str):
        self.family = family
        self.fallback = fallback
        data = path.read_bytes()
        if _u32(data, 0) not in (0x00010000, 0x74727565):
            raise ValueError(f"{path.name}: only TrueType glyf fonts are supported")
        self.t = {}
        for i in range(_u16(data, 4)):
            rec = 12 + 16 * i
            tag = data[rec : rec + 4].decode("latin-1")
            off, ln = _u32(data, rec + 8), _u32(data, rec + 12)
            self.t[tag] = data[off : off + ln]
        head = self.t["head"]
        self.upm = _u16(head, 18)
        self.num_glyphs = _u16(self.t["maxp"], 4)
        self.nhm = _u16(self.t["hhea"], 34)
        long_loca = _i16(head, 50) == 1
        loca = self.t["loca"]
        n = self.num_glyphs + 1
        self.loca = (
            list(struct.unpack_from(">%dI" % n, loca))
            if long_loca
            else [x * 2 for x in struct.unpack_from(">%dH" % n, loca)]
        )
        self.cmap = self._read_cmap()

    # -- cmap -----------------------------------------------------------
    def _read_cmap(self) -> dict:
        cm = self.t["cmap"]
        best = None
        for i in range(_u16(cm, 2)):
            pid, eid, off = _u16(cm, 4 + 8 * i), _u16(cm, 6 + 8 * i), _u32(cm, 8 + 8 * i)
            fmt = _u16(cm, off)
            rank = {(3, 10, 12): 0, (0, 4, 12): 1, (3, 1, 4): 2, (0, 3, 4): 3}.get((pid, eid, fmt))
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, off, fmt)
        if best is None:
            raise ValueError(f"{self.family}: no usable cmap subtable")
        _, off, fmt = best
        out = {}
        if fmt == 12:
            for g in range(_u32(cm, off + 12)):
                p = off + 16 + 12 * g
                start, end, gid = _u32(cm, p), _u32(cm, p + 4), _u32(cm, p + 8)
                for c in range(start, end + 1):
                    out[c] = gid + c - start
        else:
            seg = _u16(cm, off + 6) // 2
            ends = off + 14
            starts = ends + 2 * seg + 2
            deltas = starts + 2 * seg
            ranges = deltas + 2 * seg
            for s in range(seg):
                end, start = _u16(cm, ends + 2 * s), _u16(cm, starts + 2 * s)
                delta, ro = _u16(cm, deltas + 2 * s), _u16(cm, ranges + 2 * s)
                for c in range(start, end + 1):
                    if c == 0xFFFF:
                        continue
                    if ro == 0:
                        gid = (c + delta) & 0xFFFF
                    else:
                        gid = _u16(cm, ranges + 2 * s + ro + 2 * (c - start))
                        if gid:
                            gid = (gid + delta) & 0xFFFF
                    if gid:
                        out[c] = gid
        return out

    # -- metrics --------------------------------------------------------
    def _metric(self, gid: int):
        hm = self.t["hmtx"]
        if gid < self.nhm:
            return _u16(hm, 4 * gid), _i16(hm, 4 * gid + 2)
        aw = _u16(hm, 4 * (self.nhm - 1))
        return aw, _i16(hm, 4 * self.nhm + 2 * (gid - self.nhm))

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def width(self, text: str, size: float, spacing: float = 0.0) -> float:
        units = 0
        for ch in text:
            units += self._metric(self.cmap.get(ord(ch), 0))[0]
        return units * size / self.upm + spacing * len(text)

    # -- glyph data -----------------------------------------------------
    def _glyph(self, gid: int) -> bytes:
        return self.t["glyf"][self.loca[gid] : self.loca[gid + 1]]

    @staticmethod
    def _components(g: bytes):
        """Yield (offset, flags, glyph_index, length) for each component."""
        p = 10
        while True:
            flags, gi = _u16(g, p), _u16(g, p + 2)
            n = 4 + (4 if flags & 0x0001 else 2)
            n += 2 if flags & 0x0008 else 4 if flags & 0x0040 else 8 if flags & 0x0080 else 0
            yield p, flags, gi, n
            p += n
            if not flags & 0x0020:
                return

    def subset_woff(self, chars) -> bytes:
        codes = sorted({ord(c) for c in chars if ord(c) in self.cmap})
        order = [0]
        for c in codes:
            if self.cmap[c] not in order:
                order.append(self.cmap[c])
        i = 0
        while i < len(order):  # composite closure
            g = self._glyph(order[i])
            if len(g) >= 10 and _i16(g, 0) < 0:
                for _, _, gi, _ in self._components(g):
                    if gi not in order:
                        order.append(gi)
            i += 1
        remap = {old: new for new, old in enumerate(order)}

        glyf, loca, hmtx = bytearray(), [], bytearray()
        for old in order:
            loca.append(len(glyf))
            g = self._glyph(old)
            if len(g) >= 10:
                if _i16(g, 0) >= 0:  # simple glyph: drop hinting instructions
                    p = 10 + 2 * _i16(g, 0)
                    g = g[:p] + b"\0\0" + g[p + 2 + _u16(g, p) :]
                else:  # composite: remap components, drop instructions
                    out = bytearray(g[:10])
                    for p, flags, gi, n in self._components(g):
                        chunk = bytearray(g[p : p + n])
                        struct.pack_into(">HH", chunk, 0, flags & ~0x0100, remap[gi])
                        out += chunk
                    g = bytes(out)
                glyf += _pad4(g)
            aw, lsb = self._metric(old)
            hmtx += struct.pack(">Hh", aw, lsb)
        loca.append(len(glyf))

        # cmap format 4, segments of consecutive code and glyph runs
        segs = []
        for c in codes:
            gid = remap[self.cmap[c]]
            if c > 0xFFFF:
                continue
            if segs and c == segs[-1][1] + 1 and gid == segs[-1][2] + (c - segs[-1][0]):
                segs[-1][1] = c
            else:
                segs.append([c, c, gid])
        segs.append([0xFFFF, 0xFFFF, 1])
        sc = len(segs)
        es = int(math.floor(math.log2(sc)))
        sr = 2 * (2**es)
        sub = struct.pack(">HHHHHHH", 4, 0, 0, sc * 2, sr, es, sc * 2 - sr)
        sub += b"".join(struct.pack(">H", s[1]) for s in segs) + b"\0\0"
        sub += b"".join(struct.pack(">H", s[0]) for s in segs)
        sub += b"".join(struct.pack(">H", (s[2] - s[0]) & 0xFFFF if s[0] != 0xFFFF else 1) for s in segs)
        sub += b"\0\0" * sc
        sub = sub[:2] + struct.pack(">H", len(sub)) + sub[4:]
        cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub

        head = bytearray(self.t["head"])
        struct.pack_into(">I", head, 8, 0)
        struct.pack_into(">h", head, 50, 1)
        maxp = bytearray(self.t["maxp"])
        struct.pack_into(">H", maxp, 4, len(order))
        if len(maxp) >= 28:
            struct.pack_into(">H", maxp, 26, 0)  # maxSizeOfInstructions
        hhea = bytearray(self.t["hhea"])
        struct.pack_into(">H", hhea, 34, len(order))
        post = bytearray(self.t["post"][:32])
        struct.pack_into(">I", post, 0, 0x00030000)

        tables = {
            "OS/2": self.t["OS/2"],
            "cmap": cmap,
            "glyf": bytes(glyf),
            "head": bytes(head),
            "hhea": bytes(hhea),
            "hmtx": bytes(hmtx),
            "loca": struct.pack(">%dI" % len(loca), *loca),
            "maxp": bytes(maxp),
            "name": self.t["name"],
            "post": bytes(post),
        }
        # checkSumAdjustment over the equivalent sfnt
        tags = sorted(tables)
        n = len(tags)
        es = int(math.floor(math.log2(n)))
        sfnt = bytearray(struct.pack(">IHHHH", 0x00010000, n, 16 * 2**es, es, n * 16 - 16 * 2**es))
        off = 12 + 16 * n
        body = bytearray()
        for tag in tags:
            data = tables[tag]
            sfnt += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(data), off + len(body), len(data))
            body += _pad4(data)
        adj = (0xB1B0AFBA - _checksum(bytes(sfnt + body))) & 0xFFFFFFFF
        struct.pack_into(">I", head, 8, adj)
        tables["head"] = bytes(head)

        # WOFF 1.0 wrapper (zlib)
        directory, blob = bytearray(), bytearray()
        data_off = 44 + 20 * n
        total_sfnt = 12 + 16 * n
        for tag in tags:
            data = tables[tag]
            check = _checksum(data) if tag != "head" else _checksum(data[:8] + b"\0\0\0\0" + data[12:])
            comp = zlib.compress(data, 9)
            if len(comp) >= len(data):
                comp = data
            directory += struct.pack(">4sIIII", tag.encode("latin-1"), data_off + len(blob), len(comp), len(data), check)
            blob += _pad4(comp)
            total_sfnt += len(_pad4(data))
        length = data_off + len(blob)
        header = struct.pack(">4sIIHHIHHIIIII", b"wOFF", 0x00010000, length, n, 0, total_sfnt, 1, 0, 0, 0, 0, 0, 0)
        return header + bytes(directory) + bytes(blob)


FONTS: dict[str, Font] = {}


def load_fonts():
    spec = {
        "display": ("Anton-Regular.ttf", "LvxAnton", "Impact, 'Arial Narrow', sans-serif"),
        "num": ("BebasNeue-Regular.ttf", "LvxBebas", "'Arial Narrow', 'Roboto Condensed', sans-serif"),
        "body": ("Montserrat-Medium.ttf", "LvxMont", "'Segoe UI', Helvetica, Arial, sans-serif"),
        "bold": ("Montserrat-SemiBold.ttf", "LvxMontSB", "'Segoe UI', Helvetica, Arial, sans-serif"),
        "hand": ("Schoolbell-Regular.ttf", "LvxHand", "'Comic Sans MS', cursive"),
    }
    for key, (file, fam, fb) in spec.items():
        FONTS[key] = Font(FONT_DIR / file, fam, fb)


# ============================================================ svg helpers
def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def r1(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


class Doc:
    """Collects SVG body, CSS and the glyphs used per font."""

    def __init__(self, w: int, h: int, title: str, desc: str):
        self.w, self.h, self.title, self.desc = w, h, title, desc
        self.parts: list[str] = []
        self.css: list[str] = []
        self.used: dict[str, set] = {}

    def add(self, s: str):
        self.parts.append(s)

    def text(self, x, y, s, font, size, fill=OFFWHITE, spacing=0.0, anchor=None, extra="", cls=None):
        self.used.setdefault(font, set()).update(s)
        attrs = f'x="{r1(x)}" y="{r1(y)}" class="f-{font}{" " + cls if cls else ""}" font-size="{r1(size)}" fill="{fill}"'
        if spacing:
            attrs += f' letter-spacing="{r1(spacing)}"'
        if anchor:
            attrs += f' text-anchor="{anchor}"'
        if extra:
            attrs += " " + extra
        self.add(f"<text {attrs}>{esc(s)}</text>")

    def frame(self, radius=10):
        self.add(
            f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="{radius}" '
            f'fill="{BLACK}" stroke="{GRAY}"/>'
        )

    def render(self) -> str:
        faces, classes = [], []
        for key in sorted(self.used):
            f = FONTS[key]
            woff = base64.b64encode(f.subset_woff(self.used[key])).decode("ascii")
            faces.append(f"@font-face{{font-family:'{f.family}';src:url(data:font/woff;base64,{woff}) format('woff')}}")
            classes.append(f".f-{key}{{font-family:'{f.family}',{f.fallback}}}")
        css = "".join(faces + classes + self.css)
        css += "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{esc(self.title)}</title><desc id="d">{esc(self.desc)}</desc>'
            f"<style>{css}</style>" + "".join(self.parts) + "</svg>\n"
        )


def halftone(x0, y0, w, h, step, rmax, cx, cy, reach, color):
    """Halftone dots whose radius shrinks with distance from (cx, cy)."""
    dots = []
    y = y0 + step / 2
    row = 0
    while y < y0 + h:
        x = x0 + step / 2 + (step / 2 if row % 2 else 0)
        while x < x0 + w:
            d = math.hypot(x - cx, y - cy) / reach
            r = rmax * (1 - d)
            if r >= 0.35:
                dots.append(f'<circle cx="{r1(x)}" cy="{r1(y)}" r="{r1(r)}"/>')
            x += step
        y += step * 0.866
        row += 1
    return f'<g fill="{color}">' + "".join(dots) + "</g>"


def star_path(cx, cy, r):
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append(f"{r1(cx + rr * math.cos(a))},{r1(cy + rr * math.sin(a))}")
    return "M" + "L".join(pts) + "Z"


def arrow(x, y, s, color, width=2.2):
    """North-east arrow, drawn with a flat stroke."""
    return (
        f'<path d="M{r1(x)} {r1(y + s)}L{r1(x + s)} {r1(y)}M{r1(x + s * 0.3)} {r1(y)}H{r1(x + s)}V{r1(y + s * 0.7)}" '
        f'fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="square"/>'
    )


def wrap(text, font, size, max_w):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        cand = (cur + " " + wd).strip()
        if FONTS[font].width(cand, size) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def fmt_int(n: int) -> str:
    return f"{n:,}"


# ============================================================ GitHub API
QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    login
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date weekday contributionCount contributionLevel } }
      }
    }
    repositories(first: 100, after: $after, ownerAffiliations: OWNER, privacy: PUBLIC,
                 isFork: false, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        stargazerCount
        primaryLanguage { name }
        languages(first: 25, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""


def gql(token: str, variables: dict) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-cards-builder",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GitHub API HTTP {e.code}: {e.read()[:300]!r}")
    if payload.get("errors"):
        raise SystemExit(f"GitHub API error: {payload['errors']}")
    return payload["data"]


def fetch(login: str) -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Set GH_TOKEN or GITHUB_TOKEN.")
    after, user, repos = None, None, []
    while True:
        data = gql(token, {"login": login, "after": after})
        if not data.get("user"):
            raise SystemExit(f"User {login!r} not found.")
        user = user or data["user"]
        page = data["user"]["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    user["repositories"]["nodes"] = repos
    return user


# ============================================================ cards
def card_stats(u: dict) -> str:
    cc = u["contributionsCollection"]
    cal = cc["contributionCalendar"]
    total = cal["totalContributions"]
    private = cc["restrictedContributionsCount"]
    repos = u["repositories"]["nodes"]
    stars = sum(r["stargazerCount"] for r in repos)
    n_repos = u["repositories"]["totalCount"]
    pct = round(100 * private / total) if total else 0

    W, H = 840, 260
    d = Doc(W, H, "GitHub activity", f"{fmt_int(total)} contributions in the last 12 months, {pct}% of them in private work.")
    d.frame()
    d.add(halftone(0, 150, 300, 110, 9, 2.6, 0, 260, 260, "#161616"))

    # left block: the headline number
    d.text(32, 46, "CONTRIBUTIONS · LAST 12 MONTHS", "num", 17, GREEN, 1.6)
    d.text(30, 122, fmt_int(total), "display", 76, OFFWHITE, 0.5)
    note = f"{pct}% of it lives in private repos"
    d.text(34, 152, note, "hand", 17, PASTEL, extra='transform="rotate(-2 34 152)"')

    # heatmap, last 26 weeks
    weeks = cal["weeks"][-26:]
    cs, gap = 13, 3
    hx, hy = W - 32 - 26 * (cs + gap) + gap, 50
    d.text(hx, 38, "LAST 26 WEEKS", "num", 13, MUTED, 1.2)
    level_color = {
        "NONE": CELL,
        "FIRST_QUARTILE": "#0E5A05",
        "SECOND_QUARTILE": "#138A00",
        "THIRD_QUARTILE": GREEN,
        "FOURTH_QUARTILE": PASTEL,
    }
    prev_month = None
    cols = []
    for i, wk in enumerate(weeks):
        days = wk["contributionDays"]
        month = int(days[0]["date"][5:7])
        x = hx + i * (cs + gap)
        if prev_month is not None and month != prev_month and i < 24:
            d.text(x, hy + 7 * (cs + gap) + 14, MONTHS[month - 1], "num", 12, DIM, 1)
        prev_month = month
        rects = []
        for day in days:
            y = hy + day["weekday"] * (cs + gap)
            rects.append(f'<rect x="{r1(x)}" y="{r1(y)}" width="{cs}" height="{cs}" rx="2" fill="{level_color[day["contributionLevel"]]}"/>')
        cols.append(f'<g class="wk" style="animation-delay:{i * 28}ms">' + "".join(rects) + "</g>")
    d.add("".join(cols))
    d.css.append(".wk{animation:pop .5s cubic-bezier(.2,.8,.2,1) backwards}@keyframes pop{from{opacity:0}}")
    # legend, top right
    lx = W - 32 - FONTS["num"].width("MORE", 12, 1) - 8 - 5 * 13 + 3
    ly = 28
    d.text(lx - 8, ly + 9.5, "LESS", "num", 12, DIM, 1, anchor="end")
    for i, c in enumerate(level_color.values()):
        d.add(f'<rect x="{lx + i * 13}" y="{ly}" width="10" height="10" rx="2" fill="{c}"/>')
    d.text(lx + 5 * 13 + 5, ly + 9.5, "MORE", "num", 12, DIM, 1)

    # bottom metrics
    d.add(f'<line x1="32" y1="182" x2="{W - 32}" y2="182" stroke="{GRAY}"/>')
    metrics = [
        (fmt_int(cc["totalCommitContributions"]), "COMMITS", "public, past year"),
        (fmt_int(cc["totalPullRequestContributions"]), "PULL REQUESTS", "public, past year"),
        (fmt_int(n_repos), "PUBLIC REPOS", "owned, no forks"),
        (fmt_int(stars), "STARS", "on those repos"),
    ]
    colw = (W - 64) / 4
    for i, (val, label, qual) in enumerate(metrics):
        x = 32 + i * colw
        if i:
            d.add(f'<line x1="{r1(x - 14)}" y1="198" x2="{r1(x - 14)}" y2="240" stroke="{GRAY}"/>')
        d.text(x, 234, val, "num", 46, OFFWHITE, 0.5)
        vw = FONTS["num"].width(val, 46, 0.5)
        d.text(x + vw + 10, 214, label, "num", 16, GREEN, 1.2)
        d.text(x + vw + 10, 232, qual, "body", 11, MUTED)
    return d.render()


def card_langs(u: dict) -> str:
    sizes: dict[str, int] = {}
    for r in u["repositories"]["nodes"]:
        for e in r["languages"]["edges"]:
            sizes[e["node"]["name"]] = sizes.get(e["node"]["name"], 0) + e["size"]
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))
    top = ranked[:6]
    rest = sum(v for _, v in ranked[6:])
    if rest:
        top.append(("Other", rest))
    palette = [GREEN, PASTEL, OFFWHITE, "#7FD96A", "#8C8C8C", "#4A4A4A", "#2E2E2E"]

    W, H = 840, 190
    desc = ", ".join(f"{n} {100 * v / total:.1f}%" for n, v in top)
    d = Doc(W, H, "Top languages", f"Top languages by bytes in owned public repositories: {desc}.")
    d.frame()
    d.text(32, 46, "TOP LANGUAGES", "num", 17, GREEN, 1.6)
    d.text(32 + FONTS["num"].width("TOP LANGUAGES", 17, 1.6) + 10, 46, "by bytes of code in my own public repos", "body", 12, MUTED)

    bx, by, bw, bh = 32, 66, W - 64, 16
    d.add(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="3" fill="{CELL}"/>')
    d.add(f'<clipPath id="bar"><rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="3"/></clipPath>')
    segs, x = [], float(bx)
    for i, (name, v) in enumerate(top):
        w = bw * v / total
        segs.append(
            f'<rect class="seg" style="animation-delay:{120 + i * 90}ms" x="{r1(x)}" y="{by}" '
            f'width="{r1(max(w, 1.5))}" height="{bh}" fill="{palette[i]}"/>'
        )
        x += w
    d.add('<g clip-path="url(#bar)">' + "".join(segs) + "</g>")
    d.css.append(
        ".seg{transform-box:fill-box;transform-origin:left;animation:grow .7s cubic-bezier(.2,.8,.2,1) backwards}"
        "@keyframes grow{from{transform:scaleX(0)}}"
    )

    cols, colw = 4, (W - 64) / 4
    for i, (name, v) in enumerate(top):
        cx = 32 + (i % cols) * colw
        cy = 118 + (i // cols) * 40
        d.add(f'<rect x="{r1(cx)}" y="{cy - 11}" width="12" height="12" rx="2" fill="{palette[i]}"/>')
        d.text(cx + 20, cy, name, "bold", 14, OFFWHITE)
        pct = 100 * v / total
        d.text(cx + 20 + FONTS["bold"].width(name, 14) + 8, cy + 1, f"{pct:.1f}%", "num", 18, GREEN, 0.6)
    return d.render()


def card_project(idx: int, p: dict, repo: dict | None) -> str:
    name = p["repo"]
    stars = repo["stargazerCount"] if repo else 0
    lang = (repo or {}).get("primaryLanguage") or {}
    lang = lang.get("name") or p.get("lang_fallback", "")

    W, H = 420, 210
    d = Doc(W, H, name, f"{name}: {p['desc']} {p['tag'].title()}. {lang}" + (f", {stars} stars." if stars else "."))
    d.frame(8)
    d.add(halftone(W - 170, H - 100, 170, 100, 8, 2.4, W, H, 175, "#171717"))

    d.text(24, 40, f"{idx:02d}", "num", 22, GREEN, 1)

    # sticker tag, set slightly crooked
    tag = p["tag"]
    tw = FONTS["num"].width(tag, 14, 1) + 16
    tx, ty = W - 24 - tw, 20
    d.add(
        f'<g transform="rotate(-2 {r1(tx + tw / 2)} {ty + 11})"><g class="stk">'
        f'<rect x="{r1(tx + 3)}" y="{ty + 3}" width="{r1(tw)}" height="22" fill="{GREEN}"/>'
        f'<rect x="{r1(tx)}" y="{ty}" width="{r1(tw)}" height="22" fill="{PASTEL}"/>'
    )
    d.text(tx + 8, ty + 16.5, tag, "num", 14, BLACK, 1)
    d.add("</g></g>")
    # the stamp animates an inner group so it never fights the tilt above
    d.css.append(
        ".stk{transform-box:fill-box;transform-origin:center;"
        "animation:stamp .45s cubic-bezier(.3,1.4,.5,1) .15s backwards}"
        "@keyframes stamp{from{opacity:0;transform:scale(1.25) rotate(-4deg)}}"
    )

    title = name.upper()
    size = 30
    while FONTS["display"].width(title, size, 0.4) > W - 48 and size > 18:
        size -= 1
    d.text(24, 84, title, "display", size, OFFWHITE, 0.4)

    for i, line in enumerate(wrap(p["desc"], "body", 13, W - 48)[:3]):
        d.text(24, 110 + i * 19, line, "body", 13, "#CFCFCF")

    d.add(f'<line x1="24" y1="170" x2="{W - 24}" y2="170" stroke="{GRAY}"/>')
    if lang:
        d.add(f'<circle cx="30" cy="188" r="5" fill="{GREEN}"/>')
        d.text(42, 193, lang, "bold", 13, OFFWHITE)
        sx = 42 + FONTS["bold"].width(lang, 13) + 22
    else:
        sx = 24
    if stars:  # a bare zero says nothing useful, so it is left out
        d.add(f'<path d="{star_path(sx + 6, 187, 7)}" fill="{PASTEL}"/>')
        d.text(sx + 18, 194, str(stars), "num", 20, OFFWHITE, 0.6)
    d.text(W - 50, 193, "REPO", "num", 15, MUTED, 1.2, anchor="end")
    d.add(arrow(W - 40, 182, 12, GREEN))
    return d.render()


def card_arsenal() -> str:
    W = 840
    label_w = 196
    chip_h, gap, row_gap = 30, 8, 14
    pad = 22
    rows = []
    y = 70
    for label, note, items in ARSENAL:
        x = 32 + label_w
        lines = [[]]
        for it in items:
            cw = FONTS["bold"].width(it, 13) + pad
            if x + cw > W - 32:
                lines.append([])
                x = 32 + label_w
            lines[-1].append((x, it, cw))
            x += cw + gap
        rows.append((label, note, lines, y))
        y += max(len(lines) * (chip_h + gap) - gap, 46 if note else 0) + row_gap
    H = y + 18

    d = Doc(W, H, "Arsenal", "Tools I work with. " + " ".join(f"{l}: {', '.join(i)}." for l, _, i in ARSENAL))
    d.frame()
    d.add(halftone(W - 260, 0, 260, 70, 9, 2.6, W, 0, 250, "#161616"))
    d.text(32, 46, "ARSENAL", "num", 17, GREEN, 1.6)
    d.text(32 + FONTS["num"].width("ARSENAL", 17, 1.6) + 10, 46, "what I reach for, by layer", "body", 12, MUTED)

    for ri, (label, note, lines, y0) in enumerate(rows):
        if ri:
            d.add(f'<line x1="32" y1="{y0 - row_gap / 2 - 1}" x2="{W - 32}" y2="{y0 - row_gap / 2 - 1}" stroke="#161616"/>')
        d.text(32, y0 + 20, label, "num", 16, OFFWHITE, 1.1)
        if note:
            d.text(34, y0 + 40, note, "hand", 15, PASTEL, extra=f'transform="rotate(-3 34 {y0 + 40})"')
        hot = ri == 0
        edge = ri == 1
        for li, line in enumerate(lines):
            cy = y0 + li * (chip_h + gap)
            for x, it, cw in line:
                if hot:
                    fill, stroke, tc = GREEN, GREEN, BLACK
                elif edge:
                    fill, stroke, tc = PASTEL, PASTEL, BLACK
                else:
                    fill, stroke, tc = "#0D0D0D", "#2C2C2C", OFFWHITE
                d.add(f'<rect x="{r1(x + 0.5)}" y="{cy + 0.5}" width="{r1(cw - 1)}" height="{chip_h - 1}" rx="4" fill="{fill}" stroke="{stroke}"/>')
                d.text(x + pad / 2, cy + 19.5, it, "bold", 13, tc)
    return d.render()


def card_link(key: str, label: str) -> str:
    tw = FONTS["num"].width(label, 20, 1.6)
    W, H = int(24 + tw + 14 + 14 + 18), 46
    d = Doc(W, H, label.title(), f"{label.title()} link")
    d.add(f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="6" fill="{BLACK}" stroke="{GRAY}"/>')
    d.add(f'<rect x="0" y="0" width="5" height="{H}" rx="2" fill="{GREEN}"/>')
    d.text(22, 31, label, "num", 20, OFFWHITE, 1.6)
    d.add(arrow(W - 30, 16, 12, GREEN))
    return d.render()


# ============================================================ main
def build(user: dict) -> dict[str, str]:
    repos = {r["name"]: r for r in user["repositories"]["nodes"]}
    out = {"stats.svg": card_stats(user), "langs.svg": card_langs(user), "arsenal.svg": card_arsenal()}
    for i, p in enumerate(PROJECTS, 1):
        out[f"project-{p['repo']}.svg"] = card_project(i, p, repos.get(p["repo"]))
    for key, label in LINKS:
        out[f"link-{key}.svg"] = card_link(key, label)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--login", default=LOGIN)
    ap.add_argument("--from-json", type=Path, help="build from saved API data instead of calling the API")
    ap.add_argument("--dump-json", type=Path, help="save the API data to this file")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)

    user = json.loads(args.from_json.read_text("utf-8")) if args.from_json else fetch(args.login)
    if args.dump_json:
        args.dump_json.write_text(json.dumps(user, indent=1), "utf-8")

    load_fonts()
    cards = build(user)
    args.out.mkdir(parents=True, exist_ok=True)
    changed = 0
    for name, svg in cards.items():
        ET.fromstring(svg)  # must be well-formed XML
        path = args.out / name
        data = svg.encode("utf-8")
        if path.exists() and path.read_bytes() == data:
            continue
        path.write_bytes(data)
        changed += 1
        print(f"wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path} ({len(data) / 1024:.1f} KB)")
    print(f"{changed} card(s) changed, {len(cards) - changed} unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
