#!/usr/bin/env python3
"""epub-normalize-fonts: make EPUBs follow the reading app's font and size settings.

For each book it:
  * deletes embedded font files (.ttf/.otf/.woff/.woff2/...) and their OPF manifest entries
  * removes @font-face rules from CSS files and <style> blocks
  * removes every font-family declaration (monospace is kept for code)
  * converts fixed font sizes (pt, px, ...) to relative ones
  * makes the main body text exactly the reader's default size, keeping the relative
    sizes of headings, footnotes, etc.
  * removes obfuscation entries for the fonts from META-INF/encryption.xml
  * turns off Apple Books' "specified-fonts" flag

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import unquote

__version__ = "2.2.0"

# Text is decoded with surrogateescape so bytes we don't touch round-trip exactly,
# even if a file is not valid UTF-8.
ENC, ERRS = "utf-8", "surrogateescape"

FONT_EXTS = {
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".ttc", ".dfont",
    ".pfb", ".pfm", ".afm",
}
FONT_MEDIA_RE = re.compile(
    r"^\s*(font/|application/(x-)?font|application/vnd\.ms-(opentype|fontobject)"
    r"|application/x-truetype)",
    re.I,
)
# Font obfuscation (not DRM): IDPF and Adobe algorithms.
FONT_OBFUSCATION_ALGOS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}
CSS_EXTS = {".css"}
HTML_EXTS = {".xhtml", ".html", ".htm", ".xht"}

ATTR_RE = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
ROOTFILE_RE = re.compile(r"<(?:[\w-]+:)?rootfile\b[^>]*>", re.I)
MANIFEST_ITEM_RE = re.compile(
    r"[ \t]*<(?:[\w-]+:)?item\b[^>]*?(?:/>|>\s*</(?:[\w-]+:)?item\s*>)[ \t]*(?:\r?\n)?",
    re.I,
)
ENCRYPTED_DATA_RE = re.compile(
    r"[ \t]*<(?:[\w-]+:)?EncryptedData\b.*?</(?:[\w-]+:)?EncryptedData\s*>[ \t]*(?:\r?\n)?",
    re.S,
)
ENC_METHOD_ALGO_RE = re.compile(
    r"<(?:[\w-]+:)?EncryptionMethod\b[^>]*\bAlgorithm\s*=\s*[\"']([^\"']+)", re.I
)
CIPHER_URI_RE = re.compile(
    r"<(?:[\w-]+:)?CipherReference\b[^>]*\bURI\s*=\s*[\"']([^\"']+)", re.I
)


def decl_re(prop: str) -> re.Pattern:
    """Match one CSS declaration of ``prop`` (with leading whitespace and its terminator)."""
    return re.compile(
        r"(?P<lead>\s*)(?<![\w-])" + re.escape(prop) + r"\s*:\s*"
        r"(?P<value>(?:\"[^\"]*\"|'[^']*'|[^;{}\"'])*?)"
        r"(?P<end>\s*;|(?=\s*\})|\s*\Z)",
        re.I,
    )


FONT_FACE_RE = re.compile(r"[ \t]*@font-face\s*\{[^}]*\}[ \t]*(?:\r?\n)?", re.I)
FONT_FAMILY_DECL_RE = decl_re("font-family")
FONT_SIZE_DECL_RE = decl_re("font-size")
FONT_SHORTHAND_RE = decl_re("font")
IMPORTANT_RE = re.compile(r"\s*!\s*important\s*$", re.I)
FAMILY_TOKEN_RE = re.compile(r"""\s*("[^"]*"|'[^']*'|[^,]+)""")

COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
RULE_RE = re.compile(r"([^{};]*)\{([^{}]*)\}")
IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?[\"']?([^\"')\s;]+)", re.I)

STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.I | re.S)
SHEET_REF_RE = re.compile(r"<link\b[^>]*>|<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)
TAG_RE = re.compile(r"<[A-Za-z][^<>]*>")
STYLE_ATTR_RE = re.compile(r"""(\sstyle\s*=\s*)(?:"([^"]*)"|'([^']*)')""", re.I)
# Tokenizes markup; the alternatives together cover every character of the input.
TOKEN_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<[?!][^>]*>"
    r"|</\s*([\w:.-]+)\s*>"
    r"|<([\w:.-]+)((?:\"[^\"]*\"|'[^']*'|[^'\">])*)>"
    r"|[^<]+|<",
    re.S,
)

APPLE_OPTIONS = "META-INF/com.apple.ibooks.display-options.xml"
APPLE_OPTION_RE = re.compile(
    r"(<option\b[^>]*name\s*=\s*[\"']specified-fonts[\"'][^>]*>)\s*true\s*(</option>)", re.I
)
OPF_SPECIFIED_FONTS_RE = re.compile(
    r"(<(?:[\w-]+:)?meta\b[^>]*property\s*=\s*[\"']ibooks:specified-fonts[\"'][^>]*>)"
    r"\s*true\s*(</(?:[\w-]+:)?meta\s*>)",
    re.I,
)


class EpubError(Exception):
    pass


@dataclass
class Report:
    font_files: list[str] = field(default_factory=list)
    font_bytes: int = 0
    font_faces: int = 0
    declarations: int = 0
    sizes_converted: int = 0
    size_decls_removed: int = 0
    sizes_rescaled: int = 0
    line_heights_removed: int = 0
    paragraphs_changed: int = 0
    first_paragraphs: int = 0
    body_before: float | None = None
    body_after: float | None = None
    changed_files: set[str] = field(default_factory=set)

    @property
    def changed(self) -> bool:
        return bool(self.font_files or self.changed_files)

    def summary(self) -> str:
        if not self.changed:
            return "nothing to change"
        parts = []
        if self.font_files:
            parts.append(
                f"removed {len(self.font_files)} font file(s) ({human_size(self.font_bytes)})"
            )
        if self.font_faces:
            parts.append(f"{self.font_faces} @font-face rule(s)")
        if self.declarations:
            parts.append(f"{self.declarations} font-family declaration(s) changed")
        if self.sizes_converted:
            parts.append(f"{self.sizes_converted} fixed font size(s) made relative")
        if self.size_decls_removed:
            text = f"{self.size_decls_removed} body text font-size declaration(s) removed"
            if self.body_before is not None and abs(self.body_before - 1) > 0.005:
                text += f" (body text was {self.body_before:.0%})"
            parts.append(text)
        if self.sizes_rescaled:
            parts.append(f"{self.sizes_rescaled} other font size(s) rescaled to match")
        if self.line_heights_removed:
            parts.append(f"{self.line_heights_removed} line-height declaration(s) removed")
        if self.paragraphs_changed:
            text = f"{self.paragraphs_changed} paragraph(s) restyled"
            if self.first_paragraphs:
                text += f" ({self.first_paragraphs} set flush left)"
            parts.append(text)
        return ", ".join(parts) or "updated metadata"


# --------------------------------------------------------------------------- helpers


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def attrs(tag: str) -> dict[str, str]:
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in ATTR_RE.finditer(tag)
    }


def ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def resolve(base_dir: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else posixpath.normpath(href)


def norm_family(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
        name = name[1:-1]
    return " ".join(name.split()).lower()


def split_families(value: str) -> list[str]:
    return [t.strip() for t in FAMILY_TOKEN_RE.findall(value) if t.strip()]


def split_important(value: str) -> tuple[str, bool]:
    imp = IMPORTANT_RE.search(value)
    return (value[: imp.start()], True) if imp else (value, False)


def decode(data: bytes) -> str:
    return data.decode(ENC, ERRS)


def encode(text: str) -> bytes:
    return text.encode(ENC, ERRS)


def blank_comments(css: str) -> str:
    """Replace comments with spaces, keeping offsets intact."""
    return COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), css)


def iter_rules(css: str):
    """Yield (index, selector, (block_start, block_end)) for each rule block."""
    for i, m in enumerate(RULE_RE.finditer(blank_comments(css))):
        sel = re.sub(r"<!--|-->|<!\[CDATA\[|\]\]>", " ", m.group(1)).strip()
        yield i, sel, m.span(2)


def unescape_attr(raw: str) -> str:
    return (
        raw.replace("&quot;", '"').replace("&#34;", '"')
        .replace("&apos;", "'").replace("&#39;", "'")
    )


def edit_style_attrs(tag: str, fn: Callable[[str], str]) -> str:
    """Apply ``fn`` to the CSS in a tag's style attribute; drop the attribute if it empties."""

    def fix(m: re.Match) -> str:
        quote = '"' if m.group(2) is not None else "'"
        value = unescape_attr(m.group(2) if quote == '"' else m.group(3))
        new = fn(value)
        if new == value:
            return m.group(0)
        new = new.strip().strip(";").strip()
        if not new:
            return ""
        new = new.replace('"', "&quot;") if quote == '"' else new.replace("'", "&#39;")
        return f"{m.group(1)}{quote}{new}{quote}"

    return STYLE_ATTR_RE.sub(fix, tag)


def edit_css_in_html(html: str, block_fn: Callable[[str], str], inline_fn: Callable[[str], str]) -> str:
    html = STYLE_BLOCK_RE.sub(lambda m: m.group(1) + block_fn(m.group(2)) + m.group(3), html)
    return TAG_RE.sub(lambda m: edit_style_attrs(m.group(0), inline_fn), html)


# --------------------------------------------------------------------------- font families


def rewrite_families(css: str) -> tuple[str, int]:
    """Remove font-family declarations; ones that include monospace become just monospace."""
    count = 0

    def fix(m: re.Match) -> str:
        nonlocal count
        core, important = split_important(m.group("value"))
        families = split_families(core)
        kept = ["monospace"] if any(norm_family(f) == "monospace" for f in families) else []
        if kept == families:
            return m.group(0)
        count += 1
        if not kept:
            return ""
        imp = " !important" if important else ""
        return f"{m.group('lead')}font-family: {', '.join(kept)}{imp}{m.group('end')}"

    return FONT_FAMILY_DECL_RE.sub(fix, css), count


FONT_PRE_RE = re.compile(
    r"normal|italic|oblique|small-caps|bold|bolder|lighter|[1-9]00"
    r"|(?:ultra-|extra-|semi-)?(?:condensed|expanded)",
    re.I,
)
SIZE_TOKEN_RE = re.compile(
    r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[a-z]+|%)?"
    r"|xx-small|x-small|small|medium|large|x-large|xx-large|xxx-large|smaller|larger",
    re.I,
)


def parse_font_shorthand(value: str) -> dict | None:
    core, important = split_important(value)
    tokens = core.split()
    out = {"style": "normal", "variant": "normal", "weight": "normal", "line-height": "normal"}
    i = 0
    while i < len(tokens) and FONT_PRE_RE.fullmatch(tokens[i]):
        t = tokens[i].lower()
        if t in ("italic", "oblique"):
            out["style"] = t
        elif t == "small-caps":
            out["variant"] = t
        elif t in ("bold", "bolder", "lighter") or t.isdigit():
            out["weight"] = t
        i += 1
    if i >= len(tokens):
        return None
    size, _, lh = tokens[i].partition("/")
    i += 1
    if not lh and i < len(tokens) and tokens[i].startswith("/"):
        lh = tokens[i][1:]
        i += 1
        if not lh and i < len(tokens):
            lh, i = tokens[i], i + 1
    if not SIZE_TOKEN_RE.fullmatch(size) or i >= len(tokens):
        return None
    out.update(size=size, family=" ".join(tokens[i:]), important=important)
    if lh:
        out["line-height"] = lh
    return out


def expand_font_shorthand(css: str) -> str:
    """Rewrite ``font: ...`` into longhand properties so families/sizes can be edited."""

    def fix(m: re.Match) -> str:
        p = parse_font_shorthand(m.group("value"))
        if not p:
            return m.group(0)
        imp = " !important" if p["important"] else ""
        decls = "; ".join(
            f"{prop}: {p[key]}{imp}"
            for prop, key in (
                ("font-style", "style"), ("font-variant", "variant"),
                ("font-weight", "weight"), ("font-size", "size"),
                ("line-height", "line-height"), ("font-family", "family"),
            )
        )
        return f"{m.group('lead')}{decls}{m.group('end')}"

    return FONT_SHORTHAND_RE.sub(fix, css)


# --------------------------------------------------------------------------- font sizes

# Absolute units in em, taking the usual 16px = 12pt = 1em default.
ABS_UNITS = {
    "px": 1 / 16, "pt": 1 / 12, "pc": 1.0, "in": 6.0,
    "cm": 6 / 2.54, "mm": 0.6 / 2.54, "q": 0.15 / 2.54,
}
REL_UNITS = {"em": 1.0, "%": 0.01, "ex": 0.5, "ch": 0.5}
KEYWORD_SIZES = {
    "xx-small": 3 / 5, "x-small": 3 / 4, "small": 8 / 9, "medium": 1.0,
    "large": 6 / 5, "x-large": 3 / 2, "xx-large": 2.0, "xxx-large": 3.0,
}
UA_SIZES = {
    "h1": 2.0, "h2": 1.5, "h3": 1.17, "h4": 1.0, "h5": 0.83, "h6": 0.67,
    "small": 0.83, "sub": 0.83, "sup": 0.83, "big": 1.2,
}
NUM_UNIT_RE = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+))\s*([a-z%]*)", re.I)
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "source", "track", "wbr"}
NO_TEXT_TAGS = {"head", "style", "script", "title"}
SAME_SIZE_BAND = 0.08   # text within 8% of the dominant size is treated as body text
MIN_BODY_CHARS = 200


def fmt_em(x: float) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("0", "-0") else s + "em"


def convert_absolute_sizes(css: str) -> tuple[str, int]:
    """Turn font-size values in pt/px/... into em so the reader's size setting applies."""
    count = 0

    def fix(m: re.Match) -> str:
        nonlocal count
        core, important = split_important(m.group("value"))
        n = NUM_UNIT_RE.fullmatch(core.strip())
        unit = n.group(2).lower() if n else ""
        if not n or unit not in ABS_UNITS:
            return m.group(0)
        count += 1
        imp = " !important" if important else ""
        return f"{m.group('lead')}font-size: {fmt_em(float(n.group(1)) * ABS_UNITS[unit])}{imp}{m.group('end')}"

    return FONT_SIZE_DECL_RE.sub(fix, css), count


def resolve_size(value: str, parent: float) -> tuple[float, bool] | None:
    """Return (size relative to the root, is_absolute) or None to inherit."""
    v = value.strip().lower()
    if v in KEYWORD_SIZES:
        return KEYWORD_SIZES[v], True
    if v == "smaller":
        return parent / 1.2, False
    if v == "larger":
        return parent * 1.2, False
    if v == "initial":
        return 1.0, True
    m = NUM_UNIT_RE.fullmatch(v)
    if m:
        n, unit = float(m.group(1)), m.group(2)
        if unit in REL_UNITS:
            return parent * n * REL_UNITS[unit], False
        if unit == "rem":
            return n, True
        if unit in ABS_UNITS:
            return n * ABS_UNITS[unit], True
        if not unit and n == 0:
            return 0.0, True
    return None  # inherit, calc(), viewport units, ...


# Declarations the analysis follows through the cascade.
TRACKED_PROPS = {"font-size", "line-height", "text-align", "text-indent", "margin-top", "margin-bottom"}
DECL_ANY_RE = re.compile(
    r"(?<![\w-])(?P<prop>-?[a-z][\w-]*)\s*:\s*"
    r"(?P<value>(?:\"[^\"]*\"|'[^']*'|\([^)]*\)|[^;{}\"'(])*)",
    re.I,
)
LINE_HEIGHT_DECL_RE = decl_re("line-height")
NON_TEXT_RE = re.compile(r"\s+|&nbsp;|&#160;|&#xa0;|\xa0", re.I)
BLOCK_TAGS = {"p", "div"}
BODY_ALIGN = {"", "left", "justify", "start", "auto"}  # alignments of ordinary body paragraphs
PARA_SHARE = 0.02  # a paragraph style must hold 2% of the body text to count as body paragraphs


def parse_decls(block: str) -> dict[str, tuple[str, bool]]:
    """Tracked declarations of a block as {property: (value, important)}; later ones win.

    The ``margin`` shorthand is split into margin-top and margin-bottom.
    """
    out: dict[str, tuple[str, bool]] = {}
    for m in DECL_ANY_RE.finditer(block):
        prop = m.group("prop").lower()
        core, important = split_important(m.group("value"))
        core = core.strip()
        if prop == "margin":
            parts = core.split()
            if not 1 <= len(parts) <= 4:
                continue
            items = [("margin-top", parts[0]), ("margin-bottom", parts[2] if len(parts) > 2 else parts[0])]
        elif prop in TRACKED_PROPS:
            items = [(prop, core)]
        else:
            continue
        for p, v in items:
            if important or not (p in out and out[p][1]):
                out[p] = (v, important)
    return out


# Selectors: only type/class/id compounds with descendant and child combinators are
# understood. Anything else (pseudo-classes, attributes, siblings) is ignored for analysis.
Compound = tuple  # (tag or "*", frozenset(classes), id or None)
COMPOUND_RE = re.compile(r"(\*|[a-z][\w-]*)?((?:[.#][\w-]+)*)", re.I)


class Rule(NamedTuple):
    spec: tuple
    order: int
    rule_id: tuple
    comps: list
    combs: list
    decls: dict


def parse_compound(tok: str) -> Compound | None:
    m = COMPOUND_RE.fullmatch(tok)
    if not m or not tok:
        return None
    ids = re.findall(r"#([\w-]+)", m.group(2))
    return ((m.group(1) or "*").lower(), frozenset(re.findall(r"\.([\w-]+)", m.group(2))),
            ids[0] if ids else None)


def parse_selector(sel: str):
    sel = sel.strip()
    if sel.lower() == ":root":
        sel = "html"
    if not sel or re.search(r"[:\[\]+~|\\]", sel):
        return None
    comps, combs, pending = [], [], " "
    for tok in re.sub(r"\s*>\s*", " > ", sel).split():
        if tok == ">":
            pending = ">"
            continue
        c = parse_compound(tok)
        if c is None:
            return None
        if comps:
            combs.append(pending)
        comps.append(c)
        pending = " "
    if not comps or len(combs) != len(comps) - 1:
        return None
    spec = (sum(1 for c in comps if c[2]), sum(len(c[1]) for c in comps),
            sum(1 for c in comps if c[0] != "*"))
    return comps, combs, spec


def comp_match(c: Compound, el: Compound) -> bool:
    return (c[0] == "*" or c[0] == el[0]) and c[1] <= el[1] and (c[2] is None or c[2] == el[2])


def sel_matches(comps: list, combs: list, el: Compound, ancestors: list) -> bool:
    if not comp_match(comps[-1], el):
        return False

    def up(i: int, limit: int) -> bool:
        if i == 0:
            return True
        if combs[i - 1] == ">":
            j = limit - 1
            return j >= 0 and comp_match(comps[i - 1], ancestors[j]) and up(i - 1, j)
        return any(
            comp_match(comps[i - 1], ancestors[j]) and up(i - 1, j)
            for j in range(limit - 1, -1, -1)
        )

    return up(len(comps) - 1, len(ancestors))


def index_key(c: Compound) -> str:
    if c[2]:
        return "#" + c[2]
    if c[1]:
        return "." + min(c[1])
    return c[0]


def parse_sheet(css: str) -> list:
    out = []
    for idx, sel, (s, e) in iter_rules(css):
        if sel.startswith("@"):
            continue
        decls = parse_decls(css[s:e])
        if not decls:
            continue
        sels = [p for p in (parse_selector(x) for x in sel.split(",")) if p]
        if sels:
            out.append((idx, sels, decls))
    return out


def doc_sheets(doc: str, texts: dict[str, str]) -> list[tuple[tuple, str]]:
    """Stylesheets that apply to ``doc``, in cascade order, as (source_key, css)."""
    html, base = texts[doc], posixpath.dirname(doc)
    out: list[tuple[tuple, str]] = []
    seen: set[str] = set()

    def add_css(name: str) -> None:
        if name in seen or name not in texts or ext(name) not in CSS_EXTS:
            return
        seen.add(name)
        css = texts[name]
        for imp in IMPORT_RE.finditer(blank_comments(css)):
            add_css(resolve(posixpath.dirname(name), imp.group(1)))
        out.append((("css", name), css))

    k = -1
    for m in SHEET_REF_RE.finditer(html):
        if m.group(0)[:6].lower() == "<style":
            k += 1
            out.append((("style", doc, k), m.group(1) or ""))
        else:
            a = attrs(m.group(0))
            rel = a.get("rel", "").lower().split()
            if "stylesheet" in rel and "alternate" not in rel and a.get("href"):
                add_css(resolve(base, a["href"]))
    return out


def build_index(sheets: list, cache: dict) -> dict[str, list[Rule]]:
    index: dict[str, list[Rule]] = defaultdict(list)
    order = 0
    for source_key, css in sheets:
        if source_key not in cache:
            cache[source_key] = parse_sheet(css)
        for idx, sels, decls in cache[source_key]:
            for comps, combs, spec in sels:
                order += 1
                index[index_key(comps[-1])].append(
                    Rule(spec, order, ("rule", source_key, idx), comps, combs, decls)
                )
    return index


def cascade(el: Compound, ancestors: list, style: str | None, index: dict, inline_id: tuple) -> dict:
    """The winning declaration of each tracked property, as {property: (value, source)}."""
    keys = ["*", el[0], *("." + c for c in el[1])] + (["#" + el[2]] if el[2] else [])
    best: dict[str, tuple] = {}
    for k in keys:
        for r in index.get(k, ()):
            if not sel_matches(r.comps, r.combs, el, ancestors):
                continue
            for prop, (value, important) in r.decls.items():
                rank = (important, r.spec, r.order)
                if prop not in best or rank > best[prop][0]:
                    best[prop] = (rank, value, r.rule_id)
    if style:
        for prop, (value, important) in parse_decls(unescape_attr(style)).items():
            if prop not in best or important or not best[prop][0][0]:
                best[prop] = ((important, (9, 9, 9), 0), value, inline_id)
    return {prop: (value, source) for prop, (_, value, source) in best.items()}


def inherited(win: dict, prop: str, parent_value: str) -> str:
    decl = win.get(prop)
    if decl is None or decl[0].lower() in ("inherit", "unset"):
        return parent_value
    return decl[0].lower()


class Node:
    """An element as seen by the analysis."""

    __slots__ = ("doc", "ordinal", "tag", "el", "sig", "size", "chain", "adjust", "align",
                 "indent", "margin_top", "margin_bottom", "lh", "prev", "last_child", "chars")


@dataclass
class SizeStats:
    chars: Counter = field(default_factory=Counter)     # bucket -> characters of text
    weighted: Counter = field(default_factory=Counter)  # bucket -> sum of exact size x chars
    ids: dict = field(default_factory=lambda: defaultdict(set))     # bucket -> size sources
    lh_ids: dict = field(default_factory=lambda: defaultdict(set))  # bucket -> line-height sources
    blocks: list = field(default_factory=list)  # p and div Nodes


@dataclass
class SizePlan:
    """A planned change: the size sources to remove and the current body text size.

    While the plan is analyzed, ``factors`` collects, for every other relative size source,
    the factor it must be scaled by to keep its size relative to the body text.
    """

    removed: set
    body: float
    factors: dict = field(default_factory=lambda: defaultdict(Counter))


def element_size(win: dict, tag: str, parent: float, parent_chain: tuple):
    """Compute an element's font size relative to the root.

    Returns (size, chain, source, ratio). ``chain`` holds the (source, factor) pairs of the
    non-neutral size declarations that shaped the size, ``source`` is the declaration that
    set the element's own size (None if inherited) and ``ratio`` is size / parent for
    relative values (None for absolute ones).
    """
    value, source = win.get("font-size", (None, None))
    if value is None:
        return parent * UA_SIZES.get(tag, 1.0), parent_chain, None, None
    res = resolve_size(value, parent)
    if res is None:
        return parent, parent_chain, None, None
    size, absolute = res
    if absolute:
        return size, (() if abs(size - 1) < 1e-6 else ((source, size),)), source, None
    ratio = size / parent if parent else 1.0
    chain = parent_chain if abs(ratio - 1) < 1e-9 else parent_chain + ((source, ratio),)
    return size, chain, source, ratio


def plan_adjust(plan: SizePlan, source, ratio, size: float, parent: float, adjust: float) -> float:
    """Return new size / old size for an element once ``plan`` is applied.

    ``adjust`` is that value for the element's parent. Relative sources that are not removed
    are recorded with the factor that keeps them at the same size relative to the body text.
    """
    if source is None:
        return adjust
    if source in plan.removed:
        # The declaration goes away, so the element inherits its parent's new size.
        return adjust * parent / size if size else adjust
    if ratio is None:
        return 1.0  # absolute sizes are left as they are
    factor = 1 / (adjust * plan.body)
    plan.factors[source][round(factor, 4)] += 1
    return 1 / plan.body


def analyze_doc(doc: str, html: str, index: dict, stats: SizeStats, plan: SizePlan | None = None) -> None:
    stack: list[Node] = []
    top_last: Node | None = None  # last top-level element, for sibling tracking
    ordinal = -1
    for m in TOKEN_RE.finditer(html):
        end_tag, start_tag = m.group(1), m.group(2)
        if start_tag:
            ordinal += 1
            tag = start_tag.rsplit(":", 1)[-1].lower()
            a = attrs(m.group(3))
            el = (tag, frozenset(a.get("class", "").split()), a.get("id"))
            parent = stack[-1] if stack else None
            win = cascade(el, [s.el for s in stack], a.get("style"), index, ("inline", doc, ordinal))
            psize, pchain, padjust = (parent.size, parent.chain, parent.adjust) if parent else (1.0, (), 1.0)

            node = Node()
            node.doc, node.ordinal, node.tag, node.el = doc, ordinal, tag, el
            node.size, node.chain, source, ratio = element_size(win, tag, psize, pchain)
            node.adjust = plan_adjust(plan, source, ratio, node.size, psize, padjust) if plan else padjust
            node.align = inherited(win, "text-align", parent.align if parent else "")
            node.indent = inherited(win, "text-indent", parent.indent if parent else "0")
            node.margin_top = win["margin-top"][0] if "margin-top" in win else None
            node.margin_bottom = win["margin-bottom"][0] if "margin-bottom" in win else None
            lh = win.get("line-height")
            if lh is None or lh[0].lower() in ("inherit", "unset"):
                node.lh = parent.lh if parent else None
            elif lh[0].lower() in ("normal", "initial"):
                node.lh = None
            else:
                node.lh = lh[1]
            node.sig = (tag, el[1], (parent.tag, parent.el[1]) if parent else None)
            node.prev = parent.last_child if parent else top_last
            node.last_child, node.chars = None, 0
            if parent:
                parent.last_child = node
            else:
                top_last = node
            if tag in BLOCK_TAGS:
                stats.blocks.append(node)
            if not (m.group(3).rstrip().endswith("/") or tag in VOID_TAGS):
                stack.append(node)
        elif end_tag:
            tag = end_tag.rsplit(":", 1)[-1].lower()
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break
        elif stack and not m.group(0).startswith("<"):
            tags = {s.tag for s in stack}
            if "body" not in tags or tags & NO_TEXT_TAGS:
                continue
            n = len(NON_TEXT_RE.sub("", m.group(0)))
            if n:
                node = stack[-1]
                bucket = round(node.size, 2)
                stats.chars[bucket] += n
                stats.weighted[bucket] += node.size * n
                stats.ids[bucket].update(src for src, _ in node.chain)
                if node.lh is not None:
                    stats.lh_ids[bucket].add(node.lh)
                block = next((s for s in reversed(stack) if s.tag in BLOCK_TAGS), None)
                if block is not None:
                    block.chars += n


def analyze_sizes(texts: dict[str, str], plan: SizePlan | None = None) -> SizeStats:
    stats = SizeStats()
    sheet_cache: dict = {}
    index_cache: dict = {}
    for doc in (n for n in texts if ext(n) in HTML_EXTS):
        sheets = doc_sheets(doc, texts)
        key = tuple(k for k, _ in sheets)
        if key not in index_cache:
            index_cache[key] = build_index(sheets, sheet_cache)
        analyze_doc(doc, texts[doc], index_cache[key], stats, plan)
    return stats


def body_band(stats: SizeStats) -> tuple[float | None, set]:
    """The body text size and the size buckets that count as body text."""
    if sum(stats.chars.values()) < MIN_BODY_CHARS:
        return None, set()
    bucket = max(stats.chars, key=stats.chars.get)
    d = stats.weighted[bucket] / stats.chars[bucket]
    if d <= 0:
        return None, set()
    return d, {b for b in stats.chars if abs(b / d - 1) <= SAME_SIZE_BAND}


def edit_decls(css: str, factor: float | None, pattern: re.Pattern = FONT_SIZE_DECL_RE) -> tuple[str, int]:
    """Remove the declarations ``pattern`` matches (factor None) or scale relative font sizes."""
    if factor is None:
        return pattern.subn("", css)
    count = 0

    def fix(m: re.Match) -> str:
        nonlocal count
        core, important = split_important(m.group("value"))
        n = NUM_UNIT_RE.fullmatch(core.strip())
        unit = n.group(2).lower() if n else ""
        if not n or unit not in REL_UNITS:
            return m.group(0)
        count += 1
        imp = " !important" if important else ""
        value = fmt_em(float(n.group(1)) * REL_UNITS[unit] * factor)
        return f"{m.group('lead')}font-size: {value}{imp}{m.group('end')}"

    return FONT_SIZE_DECL_RE.sub(fix, css), count


def edit_rules(css: str, edits: dict[int, float | None], pattern: re.Pattern) -> tuple[str, int, int]:
    removed = rescaled = 0
    spans = [(span, edits[idx]) for idx, _, span in iter_rules(css) if idx in edits]
    for (s, e), factor in reversed(spans):
        block, n = edit_decls(css[s:e], factor, pattern)
        if factor is None:
            removed += n
        else:
            rescaled += n
        if not block.strip():
            block = " "
        css = css[:s] + block + css[e:]
    return css, removed, rescaled


def apply_edits(
    texts: dict[str, str],
    edits: dict[tuple, float | None],
    pattern: re.Pattern = FONT_SIZE_DECL_RE,
) -> tuple[int, int]:
    """Remove (value None) or scale (value = factor) the declarations of the given sources.

    ``pattern`` picks the property: font-size (which can also be scaled) or line-height.
    """
    css_targets: dict[str, dict] = defaultdict(dict)
    style_targets: dict[str, dict] = defaultdict(lambda: defaultdict(dict))
    inline_targets: dict[str, dict] = defaultdict(dict)
    for (kind, key, idx), factor in edits.items():
        if kind == "inline":
            inline_targets[key][idx] = factor
        elif key[0] == "css":
            css_targets[key[1]][idx] = factor
        else:
            style_targets[key[1]][key[2]][idx] = factor

    removed = rescaled = 0
    for name, rule_edits in css_targets.items():
        texts[name], r, s = edit_rules(texts[name], rule_edits, pattern)
        removed, rescaled = removed + r, rescaled + s

    for doc in set(style_targets) | set(inline_targets):
        html = texts[doc]
        blocks, k = style_targets.get(doc, {}), -1

        def fix_block(m: re.Match) -> str:
            nonlocal k, removed, rescaled
            k += 1
            if k not in blocks:
                return m.group(0)
            css, r, s = edit_rules(m.group(2), blocks[k], pattern)
            removed, rescaled = removed + r, rescaled + s
            return m.group(1) + css + m.group(3)

        html = STYLE_BLOCK_RE.sub(fix_block, html)

        tag_edits, counter = inline_targets.get(doc, {}), -1

        def fix_tag(m: re.Match) -> str:
            nonlocal counter
            if not m.group(2):
                return m.group(0)
            counter += 1
            if counter not in tag_edits:
                return m.group(0)
            factor = tag_edits[counter]

            def edit(css: str) -> str:
                nonlocal removed, rescaled
                new, n = edit_decls(css, factor, pattern)
                if factor is None:
                    removed += n
                else:
                    rescaled += n
                return new

            return edit_style_attrs(m.group(0), edit)

        if tag_edits:
            html = TOKEN_RE.sub(fix_tag, html)
        texts[doc] = html
    return removed, rescaled


def normalize_body_size(texts: dict[str, str]) -> tuple[int, int, float | None, float | None]:
    """Make the body text the reader's default size, without depending on any size rule.

    Finds the font size most of the book's text is rendered at and the font-size declarations
    that produce it (on the text's own elements and their ancestors). Those are removed, even
    when they cancel out (e.g. body 1.29em and p 0.77em): many reading apps override the body
    size, which would leave the text at 77%. Other relative sizes (headings, notes, ...) are
    scaled so they keep their size relative to the body text.
    Returns (declarations removed, declarations rescaled, body size before, body size after).
    """
    removed = rescaled = 0
    before = after = None
    for _ in range(8):
        stats = analyze_sizes(texts)
        d, band = body_band(stats)
        if before is None:
            before = d
        after = d
        if d is None:
            break
        ids: set = set().union(*(stats.ids[b] for b in band))
        if not ids:
            break  # the body text size no longer depends on any declaration

        plan = SizePlan(ids, d)
        analyze_sizes(texts, plan)
        edits: dict[tuple, float | None] = {i: None for i in ids}
        for source, factors in plan.factors.items():
            # Only rescale a rule that needs the same factor everywhere it applies.
            factor = factors.most_common(1)[0][0]
            if abs(factor - 1) > 0.005 and all(abs(f / factor - 1) <= 0.01 for f in factors):
                edits[source] = factor
        r, s = apply_edits(texts, edits)
        if not r:
            break
        removed, rescaled = removed + r, rescaled + s
    else:
        after = body_band(analyze_sizes(texts))[0]
    return removed, rescaled, before, after


# --------------------------------------------------------------------------- line height


def remove_absolute_line_heights(css: str) -> tuple[str, int]:
    """Drop line-height values in px/pt/...: they don't grow with the font size."""
    count = 0

    def fix(m: re.Match) -> str:
        nonlocal count
        core, _ = split_important(m.group("value"))
        n = NUM_UNIT_RE.fullmatch(core.strip())
        if not n or n.group(2).lower() not in ABS_UNITS or float(n.group(1)) == 0:
            return m.group(0)
        count += 1
        return ""

    return LINE_HEIGHT_DECL_RE.sub(fix, css), count


def normalize_line_height(texts: dict[str, str]) -> int:
    """Remove the line-height declarations that apply to the body text.

    The reading app's line spacing setting then applies. Returns the number removed.
    """
    removed = 0
    for _ in range(8):
        stats = analyze_sizes(texts)
        _, band = body_band(stats)
        ids: set = set().union(*(stats.lh_ids[b] for b in band)) if band else set()
        if not ids:
            break
        r, _ = apply_edits(texts, {i: None for i in ids}, LINE_HEIGHT_DECL_RE)
        if not r:
            break
        removed += r
    return removed


# --------------------------------------------------------------------------- paragraphs

_DECL_RES: dict[str, re.Pattern] = {}


def set_inline_props(tag: str, props: dict[str, str]) -> str:
    """Set CSS properties in a start tag's style attribute, replacing earlier values."""
    addition = "; ".join(f"{p}: {v}" for p, v in props.items())
    if STYLE_ATTR_RE.search(tag):
        def fn(css: str) -> str:
            for p in props:
                pattern = _DECL_RES.setdefault(p, decl_re(p))
                css = pattern.sub("", css)
            css = css.strip().rstrip(";").strip()
            return f"{css}; {addition}" if css else addition

        return edit_style_attrs(tag, fn)
    end = -2 if tag.endswith("/>") else -1
    return f'{tag[:end].rstrip()} style="{addition}"{tag[end:]}'


def is_length(value: str | None, target_em: float) -> bool:
    """Whether a CSS length equals ``target_em`` (0 in any unit, otherwise in em)."""
    if value is None:
        return False
    m = NUM_UNIT_RE.fullmatch(value.strip().lower())
    if not m:
        return False
    n = float(m.group(1))
    if n == 0:
        return target_em == 0
    return m.group(2) == "em" and abs(n - target_em) < 1e-6


def normalize_paragraphs(texts: dict[str, str]) -> tuple[int, int]:
    """Give body paragraphs the book convention: 1em indent, no space between paragraphs.

    Body paragraphs are p/div elements holding body-size text that are not centered or
    right-aligned, in a style (tag, classes, parent) that holds at least 2% of the body text,
    so poetry, letters and the like keep their own layout. A paragraph that does not follow
    another body paragraph (first after a heading, scene break, image, ...) is not indented.
    Returns (paragraphs changed, of which flush-left first paragraphs).
    """
    stats = analyze_sizes(texts)
    _, band = body_band(stats)
    if not band:
        return 0, 0
    paras = [n for n in stats.blocks
             if n.chars and round(n.size, 2) in band and n.align in BODY_ALIGN]
    sig_chars: Counter = Counter()
    for n in paras:
        sig_chars[n.sig] += n.chars
    total = sum(sig_chars.values())
    body = {id(n) for n in paras if sig_chars[n.sig] >= PARA_SHARE * total}

    edits: dict[str, dict[int, dict]] = defaultdict(dict)
    firsts = 0
    for n in paras:
        if id(n) not in body:
            continue
        first = n.prev is None or id(n.prev) not in body
        needs = {}
        if not (is_length(n.indent, 0.0 if first else 1.0) or (first and n.indent == "")):
            needs["text-indent"] = "0" if first else "1em"
        for prop, value in (("margin-top", n.margin_top), ("margin-bottom", n.margin_bottom)):
            if not (is_length(value, 0.0) or (value is None and n.tag == "div")):
                needs[prop] = "0"
        if needs:
            edits[n.doc][n.ordinal] = needs
            firsts += first and "text-indent" in needs

    for doc, tag_edits in edits.items():
        counter = -1

        def fix(m: re.Match) -> str:
            nonlocal counter
            if not m.group(2):
                return m.group(0)
            counter += 1
            props = tag_edits.get(counter)
            return set_inline_props(m.group(0), props) if props else m.group(0)

        texts[doc] = TOKEN_RE.sub(fix, texts[doc])
    return sum(len(e) for e in edits.values()), firsts


# --------------------------------------------------------------------------- EPUB


def check_drm(zf: zipfile.ZipFile, names: set[str]) -> None:
    if "META-INF/rights.xml" in names:
        raise EpubError("book is DRM-protected (META-INF/rights.xml present)")
    if "META-INF/encryption.xml" in names:
        text = decode(zf.read("META-INF/encryption.xml"))
        for block in ENCRYPTED_DATA_RE.finditer(text):
            algo = ENC_METHOD_ALGO_RE.search(block.group(0))
            if not algo or algo.group(1).strip() not in FONT_OBFUSCATION_ALGOS:
                raise EpubError("book is DRM-protected (encrypted content in encryption.xml)")


def find_opfs(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "META-INF/container.xml" not in names:
        raise EpubError("not a valid EPUB (META-INF/container.xml missing)")
    container = decode(zf.read("META-INF/container.xml"))
    opfs = []
    for tag in ROOTFILE_RE.findall(container):
        a = attrs(tag)
        path = a.get("full-path")
        media = a.get("media-type", "application/oebps-package+xml")
        if path and "oebps-package" in media:
            path = posixpath.normpath(unquote(path))
            if path in names:
                opfs.append(path)
    if not opfs:
        raise EpubError("not a valid EPUB (no package document found)")
    return opfs


def normalize_epub(src: Path, dst: Path | None) -> Report:
    """Process ``src``. Writes the result to ``dst`` unless it is None (dry run)."""
    report = Report()
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        names = {i.filename for i in infos}
        check_drm(zf, names)
        opfs = find_opfs(zf, names)

        # 1. Find font files: by extension anywhere, plus anything the manifest calls a font.
        fonts = {n for n in names if ext(n) in FONT_EXTS}
        opf_texts = {opf: decode(zf.read(opf)) for opf in opfs}
        for opf, text in opf_texts.items():
            base = posixpath.dirname(opf)
            for m in MANIFEST_ITEM_RE.finditer(text):
                a = attrs(m.group(0))
                href = a.get("href")
                if href and (FONT_MEDIA_RE.match(a.get("media-type", "")) or ext(href) in FONT_EXTS):
                    path = resolve(base, href)
                    if path in names:
                        fonts.add(path)

        replacements: dict[str, bytes] = {}

        # 2. Manifest entries + Apple's specified-fonts metadata.
        for opf, text in opf_texts.items():
            base = posixpath.dirname(opf)

            def drop_item(m: re.Match) -> str:
                href = attrs(m.group(0)).get("href")
                return "" if href and resolve(base, href) in fonts else m.group(0)

            new = MANIFEST_ITEM_RE.sub(drop_item, text)
            new = OPF_SPECIFIED_FONTS_RE.sub(r"\1false\2", new)
            if new != text:
                replacements[opf] = encode(new)

        if APPLE_OPTIONS in names:
            text = decode(zf.read(APPLE_OPTIONS))
            new = APPLE_OPTION_RE.sub(r"\1false\2", text)
            if new != text:
                replacements[APPLE_OPTIONS] = encode(new)

        # 3. CSS: font families, fixed sizes and line heights, then the body text size,
        #    its line height and the body paragraphs.
        texts = {n: decode(zf.read(n)) for n in names if ext(n) in CSS_EXTS | HTML_EXTS}
        originals = dict(texts)

        def css_inline(css: str) -> str:
            css = expand_font_shorthand(css)
            css, decls = rewrite_families(css)
            report.declarations += decls
            css, conv = convert_absolute_sizes(css)
            report.sizes_converted += conv
            css, lh = remove_absolute_line_heights(css)
            report.line_heights_removed += lh
            return css

        def css_sheet(css: str) -> str:
            css, faces = FONT_FACE_RE.subn("", css)
            report.font_faces += faces
            return css_inline(css)

        for name in list(texts):
            if ext(name) in CSS_EXTS:
                texts[name] = css_sheet(texts[name])
            else:
                texts[name] = edit_css_in_html(texts[name], css_sheet, css_inline)

        (report.size_decls_removed, report.sizes_rescaled,
         report.body_before, report.body_after) = normalize_body_size(texts)
        report.line_heights_removed += normalize_line_height(texts)
        report.paragraphs_changed, report.first_paragraphs = normalize_paragraphs(texts)

        for name, text in texts.items():
            if text != originals[name]:
                replacements[name] = encode(text)

        # 4. encryption.xml: drop obfuscation entries for removed fonts.
        removed_files = set(fonts)
        enc = "META-INF/encryption.xml"
        if enc in names:
            text = decode(zf.read(enc))

            def drop_enc(m: re.Match) -> str:
                uri = CIPHER_URI_RE.search(m.group(0))
                return "" if uri and resolve("", uri.group(1)) in fonts else m.group(0)

            new = ENCRYPTED_DATA_RE.sub(drop_enc, text)
            if not ENCRYPTED_DATA_RE.search(new):
                removed_files.add(enc)
            elif new != text:
                replacements[enc] = encode(new)

        report.font_files = sorted(fonts)
        report.font_bytes = sum(i.file_size for i in infos if i.filename in fonts)
        report.changed_files = set(replacements) | (removed_files - fonts)

        if dst is not None and report.changed:
            write_epub(zf, infos, replacements, removed_files, dst)
    return report


def write_epub(
    zf: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    replacements: dict[str, bytes],
    removed: set[str],
    dst: Path,
) -> None:
    # The mimetype entry must come first and be stored uncompressed.
    ordered = sorted(infos, key=lambda i: i.filename != "mimetype")
    with zipfile.ZipFile(dst, "w") as out:
        for info in ordered:
            if info.filename in removed:
                continue
            data = replacements.get(info.filename)
            if data is None:
                data = zf.read(info)
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.external_attr = info.external_attr
            if info.filename == "mimetype" or info.is_dir():
                zi.compress_type = zipfile.ZIP_STORED
            else:
                zi.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(zi, data)


# --------------------------------------------------------------------------- CLI


def collect_inputs(paths: list[str], recursive: bool, suffix: str) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            pattern = "**/*.epub" if recursive else "*.epub"
            files.extend(
                f for f in sorted(p.glob(pattern))
                if f.is_file() and not (suffix and f.stem.endswith(suffix))
            )
        else:
            files.append(p)
    return files


def output_path(src: Path, args: argparse.Namespace) -> Path:
    if args.in_place:
        return src
    if args.output_dir:
        return Path(args.output_dir) / src.name
    return src.with_name(f"{src.stem}{args.suffix}{src.suffix}")


def process(src: Path, args: argparse.Namespace) -> None:
    dst = output_path(src, args)
    label = str(src) if args.in_place or args.dry_run else f"{src} -> {dst}"

    if not args.in_place and not args.dry_run:
        if dst.resolve() == src.resolve():
            raise EpubError("output would overwrite the input; use --in-place for that")
        if dst.exists() and not args.force:
            raise EpubError(f"{dst} already exists (use --force to overwrite)")

    if args.dry_run:
        report = normalize_epub(src, None)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".epub.tmp", dir=dst.parent)
        os.close(fd)
        try:
            report = normalize_epub(src, Path(tmp))
            if report.changed:
                os.replace(tmp, dst)
            elif not args.in_place:
                # Nothing to change: still produce the output so batches are complete.
                shutil.copyfile(src, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    if not args.quiet:
        prefix = "[dry run] " if args.dry_run else ""
        print(f"{prefix}{label}: {report.summary()}")
        if args.verbose:
            for f in report.font_files:
                print(f"    - {f}")
            if report.body_before is not None and not report.size_decls_removed:
                print(f"    body text is {report.body_before:.0%} of the default size")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epub-normalize-fonts",
        description="Remove embedded fonts from EPUB files and standardize font families "
                    "and sizes, so your reader's font and size settings are used.",
    )
    p.add_argument("paths", nargs="+", help="EPUB files or directories containing EPUBs")
    out = p.add_mutually_exclusive_group()
    out.add_argument("-i", "--in-place", action="store_true", help="modify the files in place")
    out.add_argument("-o", "--output-dir", metavar="DIR", help="write cleaned books into DIR")
    p.add_argument(
        "-s", "--suffix", default="-normalized",
        help="suffix for output files written next to the input (default: %(default)s)",
    )
    p.add_argument("-r", "--recursive", action="store_true", help="search directories recursively")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would change, write nothing")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output files")
    p.add_argument("-v", "--verbose", action="store_true", help="list removed fonts and size details")
    p.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    files = collect_inputs(args.paths, args.recursive, "" if args.in_place else args.suffix)
    if not files:
        print("epub-normalize-fonts: no EPUB files found", file=sys.stderr)
        return 1

    failed = 0
    for src in files:
        try:
            process(src, args)
        except (EpubError, zipfile.BadZipFile, OSError) as e:
            failed += 1
            print(f"error: {src}: {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
