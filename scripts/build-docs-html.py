#!/usr/bin/env python
"""Render the Chinese device-testing doc as one self-contained HTML page.

Three tabs: the document, the test inventory, and the glossary. The point is
the correspondence between them -- every term the glossary defines is linked
from the first place each section mentions it, each section ends with the list
of terms it used, every glossary entry lists the sections it appears in, and
every test's method links to the term for the double it uses. That is
navigable from both ends, which markdown files sitting next to each other are
not.

The inventory tab is not written by hand. It is read out of the suite by
scripts/collect-tests.py on every build, so it cannot describe a test that no
longer exists or miss one that was just added.

    python scripts/build-docs-html.py

Writes docs/DEVICE_TESTING_zh-TW.html. Mermaid is inlined from
docs/vendor/mermaid.min.js (downloaded on first run, then cached) so the page
renders its diagrams with no network at all -- it gets opened while presenting,
and a CDN that is reachable now is not a promise about the room on the day.

The markdown subset handled here is only what these two files actually use:
headings, tables, fenced code, blockquotes, bullet lists, paragraphs, and
inline bold / code / links / section references. It is not a general converter,
and it raises rather than guesses when it meets something else.
"""

from __future__ import annotations

import html
import importlib.util
import re
import string
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "DEVICE_TESTING_zh-TW.md"
GLOSSARY = ROOT / "docs" / "GLOSSARY_zh-TW.md"
COLLECTOR = ROOT / "scripts" / "collect-tests.py"
OUT = ROOT / "docs" / "DEVICE_TESTING_zh-TW.html"

MERMAID_CACHE = ROOT / "docs" / "vendor" / "mermaid.min.js"
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

# Surfaces that would be matched constantly without carrying any meaning: they
# come from a parenthetical in a term cell, not from the term itself.
DROP_SURFACES = {"座標", "coordinates"}

# Words the document uses for a glossary entry that the glossary itself does
# not spell that way.
ALIASES = {
    "capture": ("擷取",),
    "resolve_budget": ("時間預算",),
    "provenance": ("來歷",),
    "content-desc": ("content_desc",),
    "resource-id": ("resource_id",),
    "Spy": ("Stub",),
    "adb shell": ("adb.shell",),
    "UI Automator": ("uiautomator",),
    "便宜的先讀": ("便宜的讀取先做",),
}

CJK_DIGITS = "〇一二三四五六七八九"


def cjk_number(text: str) -> int | None:
    """`十一` -> 11. Section headings are numbered this way in both files."""
    if not text or any(c not in CJK_DIGITS + "十" for c in text):
        return None
    if "十" not in text:
        return sum(CJK_DIGITS.index(c) for c in text) if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    return (CJK_DIGITS.index(tens) if tens else 1) * 10 + (
        CJK_DIGITS.index(ones) if ones else 0
    )


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slug(text: str) -> str:
    out = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-")
    return out.lower() or "x"


# --------------------------------------------------------------------------
# glossary model


@dataclass
class Entry:
    eid: str
    term_html: str
    term_text: str
    cells: list[tuple[str, str]]          # (column header, cell markdown)
    surfaces: list[str]
    section: str                          # glossary section title
    backrefs: list[str] = field(default_factory=list)   # doc section ids

    @property
    def tip(self) -> str:
        body = self.cells[0][1] if self.cells else ""
        body = re.sub(r"[*`]", "", body).replace("<br>", " ")
        return body[:110] + ("…" if len(body) > 110 else "")


@dataclass
class GlossarySection:
    gid: str
    number: str
    title: str
    headers: list[str]
    entries: list[Entry]


@dataclass
class DocSection:
    sid: str
    label: str                            # "§七", or "" for the front matter
    title: str
    lines: list[str]
    used: list[Entry] = field(default_factory=list)     # every term mentioned
    linked: set[str] = field(default_factory=set)       # ...of those, linked once


class Context:
    """Carries the term index plus which terms this section already linked."""

    def __init__(self, entries: list[Entry]) -> None:
        self.by_code: dict[str, Entry] = {}
        surfaces: list[tuple[str, Entry]] = []
        for entry in entries:
            for surface in entry.surfaces:
                surfaces.append((surface, entry))
                self.by_code.setdefault(surface, entry)
        # Longest first so `uiautomator dump` wins over `dump`.
        self.surfaces = sorted(surfaces, key=lambda pair: -len(pair[0]))
        self.section: DocSection | None = None
        self.linking = False

    def mark(self, entry: Entry, link: bool = True) -> bool:
        """Record a mention; True only when this one should become a link.

        A term can be mentioned inside a mermaid diagram, where there is no
        markup to hang a link on. Those still count as mentions -- they get the
        term into the section's list and into the entry's back-references --
        but they must not spend the section's one link, or a diagram at the top
        of a section would silently disarm the prose below it.
        """
        if not self.linking or self.section is None:
            return False
        if entry not in self.section.used:
            self.section.used.append(entry)
            if self.section.label and self.section.label not in entry.backrefs:
                entry.backrefs.append(self.section.label)
        if not link or entry.eid in self.section.linked:
            return False
        self.section.linked.add(entry.eid)
        return True

    def detect(self, text: str) -> None:
        """Mentions inside a fenced block: counted, never linked."""
        for surface, entry in self.surfaces:
            if find_surface(text, surface) >= 0:
                self.mark(entry, link=False)


# --------------------------------------------------------------------------
# markdown -> html

CODE_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SECREF_RE = re.compile(r"§([〇一二三四五六七八九十]+)")
TAG_RE = re.compile(r"<[^>]+>")
IDENT = set(string.ascii_letters + string.digits + "_")


def find_surface(hay: str, surface: str) -> int:
    """Where `surface` starts in `hay`, or -1.

    ASCII terms match case-insensitively but only on identifier boundaries, so
    `run` does not light up inside `runner` and `adb` does not inside `adbx`.
    """
    if surface.isascii():
        lowered, needle = hay.lower(), surface.lower()
        start = 0
        while True:
            i = lowered.find(needle, start)
            if i < 0:
                return -1
            before = hay[i - 1] if i else ""
            after = hay[i + len(surface)] if i + len(surface) < len(hay) else ""
            if before not in IDENT and after not in IDENT:
                return i
            start = i + 1
    return hay.find(surface)


def term_link(inner: str, entry: Entry) -> str:
    return (
        f'<a class="term" href="#{entry.eid}" '
        f'data-tip="{html.escape(entry.tip, quote=True)}" '
        f'data-term="{html.escape(entry.term_text, quote=True)}">{inner}</a>'
    )


def link_run(text: str, ctx: Context) -> str:
    """Link the first mention of each still-unlinked term in one text run."""
    out: list[str] = []
    while text:
        best: tuple[int, int, Entry] | None = None
        for surface, entry in ctx.surfaces:
            if ctx.section is not None and entry.eid in ctx.section.linked:
                continue
            i = find_surface(text, surface)
            if i < 0:
                continue
            if best is None or i < best[0] or (i == best[0] and len(surface) > best[1]):
                best = (i, len(surface), entry)
        if best is None:
            break
        i, length, entry = best
        if not ctx.mark(entry):
            break
        out.append(text[:i])
        out.append(term_link(text[i : i + length], entry))
        text = text[i + length :]
    out.append(text)
    return "".join(out)


def link_terms(fragment: str, ctx: Context) -> str:
    """Same, over a fragment that already has tags -- never inside an anchor."""
    out: list[str] = []
    pos = depth = 0
    for m in TAG_RE.finditer(fragment):
        chunk = fragment[pos : m.start()]
        out.append(chunk if depth else link_run(chunk, ctx))
        tag = m.group(0)
        if tag.startswith("<a"):
            depth += 1
        elif tag.startswith("</a"):
            depth = max(0, depth - 1)
        out.append(tag)
        pos = m.end()
    chunk = fragment[pos:]
    out.append(chunk if depth else link_run(chunk, ctx))
    return "".join(out)


BOLD_OPEN, BOLD_CLOSE = "\x01", "\x02"


def mark_bold(text: str) -> str:
    """Turn `**` pairs into sentinels, ignoring any inside a code span.

    Doing this before the code spans are split out is what lets a bold run
    contain one -- `**§七** 的 `resolve_budget`` -- which the document does
    constantly.
    """
    spans = [(m.start(), m.end()) for m in CODE_RE.finditer(text)]
    marks = [
        m.start()
        for m in re.finditer(r"\*\*", text)
        if not any(a <= m.start() < b for a, b in spans)
    ]
    if len(marks) < 2:
        return text
    out, last, opening = [], 0, True
    for i in marks[: len(marks) // 2 * 2]:
        out.append(text[last:i])
        out.append(BOLD_OPEN if opening else BOLD_CLOSE)
        opening = not opening
        last = i + 2
    out.append(text[last:])
    return "".join(out)


def inline(text: str, ctx: Context, link: bool = True) -> str:
    text = mark_bold(text)
    pieces: list[tuple[str, str]] = []
    pos = 0
    for m in CODE_RE.finditer(text):
        pieces.append(("text", text[pos : m.start()]))
        pieces.append(("code", m.group(1)))
        pos = m.end()
    pieces.append(("text", text[pos:]))

    out: list[str] = []
    for kind, raw in pieces:
        if kind == "code":
            entry = ctx.by_code.get(raw) if link else None
            if entry is not None and ctx.mark(entry):
                out.append(term_link(f"<code>{esc(raw)}</code>", entry))
            elif link and entry is None:
                # `resolve_budget < adapter_action_timeout` is two terms and an
                # operator, so an exact lookup on the whole span finds neither.
                out.append(f"<code>{link_run(esc(raw), ctx)}</code>")
            else:
                out.append(f"<code>{esc(raw)}</code>")
            continue
        body = esc(raw).replace("&lt;br&gt;", "<br>")
        body = body.replace(BOLD_OPEN, "<strong>").replace(BOLD_CLOSE, "</strong>")
        body = LINK_RE.sub(_md_link, body)
        body = SECREF_RE.sub(_sec_link, body)
        out.append(link_terms(body, ctx) if link else body)
    return "".join(out)


def _md_link(m: re.Match[str]) -> str:
    return f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>'


def _sec_link(m: re.Match[str]) -> str:
    number = cjk_number(m.group(1))
    if number is None:
        return m.group(0)
    return f'<a class="secref" href="#sec-{number}">§{m.group(1)}</a>'


def split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


ALIGN_RE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")


def alignments(line: str) -> list[str]:
    out = []
    for cell in split_row(line):
        if cell.endswith(":") and cell.startswith(":"):
            out.append("center")
        elif cell.endswith(":"):
            out.append("right")
        else:
            out.append("left")
    return out


def render_blocks(lines: list[str], ctx: Context, link: bool = True) -> str:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if line.startswith("```"):
            lang = line[3:].strip()
            body: list[str] = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            source = "\n".join(body)
            if link:
                ctx.detect(source)
            if lang == "mermaid":
                # textContent round-trips the escaping, so mermaid sees the
                # source byte for byte -- including its own `&lt;`.
                out.append(
                    '<figure class="diagram"><pre class="mermaid">'
                    f"{esc(source)}</pre></figure>"
                )
            else:
                out.append(
                    f'<pre class="code" data-lang="{html.escape(lang, quote=True)}">'
                    f"<code>{esc(source)}</code></pre>"
                )
            continue

        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            text = line[level:].strip()
            if link:
                ctx.detect(text)      # a heading counts as a mention, not a link
            out.append(f"<h{level}>{inline(text, ctx, link=False)}</h{level}>")
            i += 1
            continue

        if line.strip() in {"---", "***", "___"}:
            i += 1                      # section boundaries carry this already
            continue

        if line.startswith("|") and i + 1 < n and ALIGN_RE.match(lines[i + 1]):
            head = split_row(line)
            align = alignments(lines[i + 1])
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            out.append(render_table(head, align, rows, ctx, link))
            continue

        if line.startswith(">"):
            quoted: list[str] = []
            while i < n and lines[i].startswith(">"):
                quoted.append(lines[i][1:].lstrip(" "))
                i += 1
            out.append(
                f"<blockquote>{render_blocks(quoted, ctx, link)}</blockquote>"
            )
            continue

        if line.startswith("- "):
            items: list[str] = []
            while i < n and lines[i].startswith("- "):
                items.append(lines[i][2:].strip())
                i += 1
            body = "".join(f"<li>{inline(item, ctx, link)}</li>" for item in items)
            out.append(f"<ul>{body}</ul>")
            continue

        para: list[str] = []
        while i < n and lines[i].strip() and not lines[i].lstrip().startswith(
            ("#", ">", "- ", "|", "```", "---")
        ):
            para.append(lines[i].strip())
            i += 1
        joined = "<br>".join(inline(p, ctx, link) for p in para)
        out.append(f"<p>{joined}</p>")
    return "".join(out)


def render_table(
    head: list[str],
    align: list[str],
    rows: list[list[str]],
    ctx: Context,
    link: bool,
) -> str:
    def cell(tag: str, text: str, idx: int) -> str:
        a = align[idx] if idx < len(align) else "left"
        return f'<{tag} class="a-{a}">{inline(text, ctx, link)}</{tag}>'

    thead = "".join(cell("th", text, k) for k, text in enumerate(head))
    body = "".join(
        "<tr>" + "".join(cell("td", text, k) for k, text in enumerate(row)) + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table></div>"
    )


# --------------------------------------------------------------------------
# parsing the two files


def surfaces_for(term_cell: str) -> tuple[str, list[str]]:
    """Every string in the document that should link to this entry."""
    plain = term_cell.replace("**", "").replace("<br>", " ").strip()
    found: list[str] = []
    for part in plain.split("/"):
        part = part.strip().strip("`").strip()
        if not part or part == "—":
            continue
        outer = re.sub(r"（[^）]*）", "", part).strip()
        inner = re.findall(r"（([^）]*)）", part)
        for candidate in [outer, *inner]:
            candidate = candidate.strip().strip("`").strip()
            if len(candidate) < 2 or candidate in DROP_SURFACES:
                continue
            if candidate not in found:
                found.append(candidate)
    primary = found[0] if found else plain
    for extra in ALIASES.get(primary, ()):
        if extra not in found:
            found.append(extra)
    return plain, found


def parse_glossary(text: str) -> list[GlossarySection]:
    sections: list[GlossarySection] = []
    current: GlossarySection | None = None
    lines = text.splitlines()
    i, n = 0, len(lines)
    seen_ids: set[str] = set()

    while i < n:
        line = lines[i]
        if line.startswith("## "):
            title = line[3:].strip()
            number, _, name = title.partition("、")
            current = GlossarySection(
                gid=f"gs-{cjk_number(number) if cjk_number(number) is not None else len(sections)}",
                number=number,
                title=name or title,
                headers=[],
                entries=[],
            )
            sections.append(current)
            i += 1
            continue

        if current is not None and line.startswith("|") and i + 1 < n and ALIGN_RE.match(lines[i + 1]):
            current.headers = split_row(line)
            i += 2
            while i < n and lines[i].startswith("|"):
                cells = split_row(lines[i])
                term_cell = cells[0]
                term_text, found = surfaces_for(term_cell)
                eid = f"g-{slug(found[0] if found else term_text)}"
                while eid in seen_ids:
                    eid += "-2"
                seen_ids.add(eid)
                current.entries.append(
                    Entry(
                        eid=eid,
                        term_html="",
                        term_text=term_text,
                        cells=[
                            (current.headers[k] if k < len(current.headers) else "", cells[k])
                            for k in range(1, len(cells))
                        ],
                        surfaces=found,
                        section=current.title,
                    )
                )
                i += 1
            continue
        i += 1
    return sections


def parse_doc(text: str) -> tuple[str, list[DocSection]]:
    lines = text.splitlines()
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), "AutoPerf")
    sections: list[DocSection] = []
    current = DocSection(sid="sec-front", label="", title="", lines=[])
    for line in lines:
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            sections.append(current)
            heading = line[3:].strip()
            number, _, name = heading.partition("、")
            value = cjk_number(number)
            current = DocSection(
                sid=f"sec-{value}" if value is not None else f"sec-{slug(heading)}",
                label=f"§{number}" if value is not None else "",
                title=name or heading,
                lines=[],
            )
            continue
        current.lines.append(line)
    sections.append(current)
    return title, [s for s in sections if s.lines or s.title]


# --------------------------------------------------------------------------
# assembling the page


def mermaid_source() -> str:
    if not MERMAID_CACHE.exists():
        MERMAID_CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {MERMAID_URL}")
        with urllib.request.urlopen(MERMAID_URL, timeout=120) as response:
            MERMAID_CACHE.write_bytes(response.read())
    source = MERMAID_CACHE.read_text(encoding="utf-8")
    # Only a literal `</script` would end the inline block early.
    return source.replace("</script", "<\\/script")


def render_doc(sections: list[DocSection], ctx: Context) -> str:
    parts: list[str] = []
    for section in sections:
        ctx.section = section
        ctx.linking = True
        body = render_blocks(section.lines, ctx)
        heading = ""
        if section.title:
            label = f'<span class="sec-label">{section.label}</span>' if section.label else ""
            title = inline(section.title, ctx, link=False)
            heading = f'<h2 id="{section.sid}">{label}{title}</h2>'
        strip = ""
        if section.used and section.label:
            links = " ".join(
                f'<a class="chip" href="#{e.eid}">{inline(e.term_text, ctx, link=False)}</a>'
                for e in section.used
            )
            strip = (
                '<aside class="term-strip"><span class="term-strip-label">'
                "本節名詞 → 名詞解釋分頁</span>"
                f'<span class="chips">{links}</span></aside>'
            )
        anchor = "" if section.title else f'<span id="{section.sid}"></span>'
        parts.append(f'<section class="doc-section">{anchor}{heading}{body}{strip}</section>')
    ctx.section = None
    ctx.linking = False
    return "".join(parts)


def render_glossary(sections: list[GlossarySection], ctx: Context) -> str:
    parts: list[str] = []
    for gs in sections:
        cards: list[str] = []
        for entry in gs.entries:
            rows = "".join(
                f"<div class=\"row\"><dt>{esc(label)}</dt>"
                f"<dd>{inline(value, ctx, link=False)}</dd></div>"
                for label, value in entry.cells
                if value.strip() and value.strip() != "—"
            )
            if entry.backrefs:
                refs = "、".join(
                    f'<a class="secref" href="#sec-{cjk_number(r[1:])}">{r}</a>'
                    for r in entry.backrefs
                    if cjk_number(r[1:]) is not None
                )
                rows += (
                    '<div class="row backrefs"><dt>文件出處</dt>'
                    f"<dd>{refs}</dd></div>"
                )
            search = html.escape(
                " ".join([entry.term_text, *(v for _, v in entry.cells)]), quote=True
            )
            cards.append(
                f'<article class="card" id="{entry.eid}" data-search="{search}">'
                f'<h4>{inline(entry.term_text, ctx, link=False)}</h4>'
                f"<dl>{rows}</dl></article>"
            )
        label = f'<span class="sec-label">§{gs.number}</span>' if gs.number else ""
        parts.append(
            f'<section class="gloss-section" data-gs="{gs.gid}">'
            f'<h2 id="{gs.gid}">{label}{esc(gs.title)}</h2>'
            f'<div class="cards">{"".join(cards)}</div></section>'
        )
    return "".join(parts)


# --------------------------------------------------------------------------
# the test inventory tab


def load_inventory() -> dict:
    """Every test, read out of the suite itself by scripts/collect-tests.py."""
    spec = importlib.util.spec_from_file_location("collect_tests", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect()


def group_tests(tests: list[dict]) -> dict[str, dict[str, dict[str, list[dict]]]]:
    """group -> module -> suite -> tests, keeping the order they arrived in."""
    tree: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for test in tests:
        modules = tree.setdefault(test["group"], {})
        suites = modules.setdefault(test["module"], {})
        suites.setdefault(test["suite"], []).append(test)
    return tree


def technique_chip(technique: dict, ctx: Context) -> str:
    """A method label, linked to its glossary entry when it has one."""
    label = esc(technique["label"])
    tip = html.escape(technique["why"], quote=True)
    entry = ctx.by_code.get(technique.get("term", ""))
    if entry is None:
        return f'<span class="tech" title="{tip}">{label}</span>'
    return (f'<a class="tech tech-link" href="#{entry.eid}" data-tip="{tip}" '
            f'data-term="{html.escape(entry.term_text, quote=True)}">{label}</a>')


# The split the inventory leads with. Which side a test lands on is decided
# per test, from what its body names -- see DEVICE_MARKERS in collect-tests.py.
PARTS = [
    ("device", "裝置測項",
     "拿掉裝置這個概念，這些測試就沒有意義了：它們驅動 adapter、解析裝置吐出來的文字、"
     "或是對 selector 表下判斷。每一列的「判定依據」就是它被歸到這邊的證據。"),
    ("other", "非裝置測項",
     "沒有任何裝置也照樣成立：儲存、統計、佇列派送、API 形狀、程序監控。"
     "它們絕對不會因為換一支手機或 YouTube 改版而紅 —— 出事的時候，"
     "先知道這一大塊跟你無關，本身就值錢。"),
]


def render_tests(inventory: dict, ctx: Context) -> str:
    out: list[str] = []
    if inventory.get("warning"):
        out.append(
            '<p class="error">這份清單缺了 webapp 那一半：'
            f'{esc(inventory["warning"])}</p>'
        )
    for key, title, why in PARTS:
        wanted = [t for t in inventory["tests"] if bool(t["device"]) == (key == "device")]
        if not wanted:
            continue
        body = render_part(wanted, inventory["groups"], ctx, device=key == "device")
        out.append(
            f'<section class="test-part" data-part="{key}" id="tp-{key}">'
            f'<h2 id="tp-{key}-h" class="part-head">{esc(title)}'
            f'<span class="count big">{len(wanted)}</span></h2>'
            f'<p class="group-why">{esc(why)}</p>{body}</section>'
        )
    return "".join(out)


def render_part(tests: list[dict], groups: list[dict], ctx: Context, device: bool) -> str:
    tree = group_tests(tests)
    part = "device" if device else "other"
    counts = {group: sum(len(t) for s in modules.values() for t in s.values())
              for group, modules in tree.items()}
    parts: list[str] = []

    for group in groups:
        name = group["name"]
        modules = tree.get(name)
        if not modules:
            continue
        blocks: list[str] = []
        for module, suites in modules.items():
            module_purpose = next(iter(next(iter(suites.values()))))["module_purpose"]
            suite_blocks = []
            for suite, tests in suites.items():
                rows = []
                for test in tests:
                    chips = " ".join(technique_chip(x, ctx) for x in test["techniques"])
                    # Only 32 of these have a docstring. The rest state their
                    # purpose in the name -- the names in this suite are whole
                    # sentences -- so that is what the column shows, marked as
                    # derived rather than quietly presented as prose someone
                    # wrote.
                    purpose = (inline(test["purpose"], ctx, link=False)
                               if test["purpose"] else
                               f'<span class="derived">{esc(test["title"])}</span>')
                    search = html.escape(
                        " ".join([test["name"], test["title"], test["purpose"],
                                  test["suite"], module,
                                  *(x["label"] for x in test["techniques"])]),
                        quote=True,
                    )
                    evidence = (f'<td class="t-why">{esc(test["why_device"])}</td>'
                                if device else "")
                    rows.append(
                        f'<tr data-search="{search}">'
                        f'<td class="t-name"><code>{esc(test["name"])}</code></td>'
                        f"<td>{purpose}</td>"
                        f'<td class="t-how">{chips}</td>{evidence}</tr>'
                    )
                purpose_line = (
                    f'<p class="hint">{inline(tests[0]["suite_purpose"], ctx, link=False)}</p>'
                    if tests[0]["suite_purpose"] else "")
                suite_blocks.append(
                    f'<div class="suite"><h4>{esc(suite)}'
                    f'<span class="count">{len(tests)}</span></h4>{purpose_line}'
                    '<div class="table-wrap"><table><thead><tr>'
                    "<th>測試項目</th><th>測試目的</th><th>測試方法</th>"
                    + ("<th>判定依據</th>" if device else "")
                    + f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
                )
            total = sum(len(t) for t in suites.values())
            module_line = (f'<p class="hint">{inline(module_purpose, ctx, link=False)}</p>'
                           if module_purpose else "")
            blocks.append(
                f'<section class="test-module" id="tm-{part}-{slug(name)}-{slug(module)}">'
                f'<h3><code>{esc(module)}</code><span class="count">{total}</span></h3>'
                f"{module_line}{''.join(suite_blocks)}</section>"
            )
        parts.append(
            f'<section class="test-group" id="tg-{part}-{slug(name)}">'
            f'<h3 class="group-head" id="tg-{part}-{slug(name)}-h">'
            f'<span class="sec-label">{esc(name)}</span>'
            f'{counts[name]} 個測試</h3>'
            f'<p class="group-why">{inline(group["why"], ctx, link=False)}</p>'
            f"{''.join(blocks)}</section>"
        )
    return "".join(parts)


def tests_toc(inventory: dict) -> str:
    items = []
    for key, title, _ in PARTS:
        wanted = [t for t in inventory["tests"] if bool(t["device"]) == (key == "device")]
        if not wanted:
            continue
        items.append(
            f'<a class="toc-part" href="#tp-{key}-h"><span class="n">■</span>{esc(title)}'
            f'<span class="count">{len(wanted)}</span></a>'
        )
        tree = group_tests(wanted)
        for group in inventory["groups"]:
            modules = tree.get(group["name"])
            if not modules:
                continue
            count = sum(len(t) for s in modules.values() for t in s.values())
            items.append(
                f'<a class="toc-sub" href="#tg-{key}-{slug(group["name"])}-h">'
                f'<span class="n">{esc(group["name"])}</span>'
                f'<span class="count">{count}</span></a>'
            )
    return f'<nav class="toc">{"".join(items)}</nav>'


def toc(sections: list[DocSection], ctx: Context) -> str:
    items = "".join(
        f'<a href="#{s.sid}"><span class="n">{s.label or "·"}</span>'
        f"{inline(s.title, ctx, link=False)}</a>"
        for s in sections
        if s.title
    )
    return f'<nav class="toc">{items}</nav>'


def gloss_toc(sections: list[GlossarySection]) -> str:
    items = "".join(
        f'<a href="#{gs.gid}"><span class="n">§{gs.number}</span>{esc(gs.title)}'
        f'<span class="count">{len(gs.entries)}</span></a>'
        for gs in sections
    )
    return f'<nav class="toc">{items}</nav>'


def build() -> None:
    gloss_sections = parse_glossary(GLOSSARY.read_text(encoding="utf-8"))
    entries = [e for gs in gloss_sections for e in gs.entries]
    ctx = Context(entries)

    title, doc_sections = parse_doc(DOC.read_text(encoding="utf-8"))
    doc_html = render_doc(doc_sections, ctx)
    gloss_html = render_glossary(gloss_sections, ctx)
    inventory = load_inventory()
    tests_html = render_tests(inventory, ctx)

    linked = sum(1 for e in entries if e.backrefs)
    page = PAGE.format(
        title=esc(title),
        doc_toc=toc(doc_sections, ctx),
        gloss_toc=gloss_toc(gloss_sections),
        tests_toc=tests_toc(inventory),
        doc=doc_html,
        glossary=gloss_html,
        tests=tests_html,
        term_count=len(entries),
        linked_count=linked,
        test_count=len(inventory["tests"]),
        mermaid=mermaid_source(),
        css=CSS,
        js=JS,
    )
    OUT.write_text(page, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size / 1024:.0f} KB")
    print(f"  {len(doc_sections)} sections, {len(entries)} terms, {linked} linked from the doc")
    print(f"  {len(inventory['tests'])} tests in "
          f"{len({t['module'] for t in inventory['tests']})} modules")
    if inventory.get("warning"):
        print(f"  INCOMPLETE -- webapp half missing: {inventory['warning']}")
    unlinked = [e.term_text for e in entries if not e.backrefs]
    if unlinked:
        print(f"  never mentioned in the doc: {', '.join(unlinked)}")


CSS = """
:root{
  --paper:#faf9f7; --card:#fff; --ink:#1d2422; --muted:#6b7472; --line:#e2e0da;
  --teal:#12726b; --teal-soft:#dcebe8; --clay:#9c3b22; --clay-soft:#f3e3dd;
  --sand:#8a6410; --sand-soft:#f3ebd8; --stone:#eaece7;
  --mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace;
  --sans:-apple-system,"Segoe UI","PingFang TC","Microsoft JhengHei",
         "Noto Sans TC",system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:16px;line-height:1.85;-webkit-font-smoothing:antialiased}
a{color:var(--teal)}

.topbar{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:24px;
  padding:0 24px;height:58px;background:rgba(250,249,247,.92);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.brand{font-weight:700;letter-spacing:.14em;font-size:13px;color:var(--teal)}
.brand span{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--teal);margin-right:8px}
.tabs{display:flex;gap:4px}
.tabs button{font:inherit;font-size:14px;font-weight:600;color:var(--muted);
  background:none;border:0;border-radius:8px;padding:7px 15px;cursor:pointer}
.tabs button:hover{background:var(--stone);color:var(--ink)}
.tabs button.active{background:var(--teal);color:#fff}
.topmeta{margin-left:auto;font-size:12px;color:var(--muted);
  font-family:var(--mono)}
@media(max-width:720px){.topmeta{display:none}}

.layout{display:grid;grid-template-columns:250px minmax(0,1fr);gap:40px;
  max-width:1240px;margin:0 auto;padding:0 24px 120px}
@media(max-width:960px){.layout{grid-template-columns:1fr;gap:0}
  .side{position:static!important;height:auto!important;padding-top:16px}}
.side{position:sticky;top:58px;height:calc(100vh - 58px);overflow-y:auto;
  padding:28px 0 40px}
.toc{display:flex;flex-direction:column;gap:1px;font-size:13.5px}
.toc a{display:flex;gap:9px;align-items:baseline;text-decoration:none;
  color:var(--muted);padding:5px 10px;border-radius:7px;line-height:1.5}
.toc a:hover{background:var(--stone);color:var(--ink)}
.toc a.active{background:var(--teal-soft);color:var(--teal);font-weight:600}
.toc .n{font-family:var(--mono);font-size:11.5px;opacity:.7;min-width:26px}
.toc .count{margin-left:auto;font-family:var(--mono);font-size:11px;opacity:.6}
.search{width:100%;font:inherit;font-size:13.5px;padding:8px 11px;
  border:1px solid var(--line);border-radius:8px;background:var(--card);
  margin-bottom:14px}
.search:focus{outline:2px solid var(--teal-soft);border-color:var(--teal)}

main{padding-top:34px;min-width:0}
.panel{display:none}
.panel.active{display:block}
h1{font-size:30px;line-height:1.35;margin:0 0 6px}
h2{font-size:23px;line-height:1.4;margin:56px 0 18px;padding-top:14px;
  border-top:1px solid var(--line);scroll-margin-top:74px}
.doc-section:first-child h2,.gloss-section:first-child h2{border-top:0;margin-top:8px}
h3{font-size:17.5px;margin:34px 0 12px;color:var(--teal)}
h4{font-size:15px;margin:0 0 10px}
.sec-label{display:inline-block;font-family:var(--mono);font-size:13px;
  color:var(--teal);background:var(--teal-soft);border-radius:6px;
  padding:2px 8px;margin-right:11px;vertical-align:middle}
p{margin:14px 0}
ul{margin:14px 0;padding-left:20px}
li{margin:8px 0}
strong{font-weight:650}
code{font-family:var(--mono);font-size:.87em;background:var(--stone);
  padding:1.5px 5px;border-radius:5px;word-break:break-word}
blockquote{margin:20px 0;padding:14px 20px;background:var(--card);
  border-left:3px solid var(--teal);border-radius:0 10px 10px 0;
  color:#39423f;box-shadow:0 1px 2px rgba(0,0,0,.03)}
blockquote p:first-child{margin-top:0} blockquote p:last-child{margin-bottom:0}
pre.code{background:#16201e;color:#e8ece9;padding:16px 18px;border-radius:11px;
  overflow-x:auto;font-size:13px;line-height:1.7;position:relative}
pre.code code{background:none;padding:0;color:inherit;font-size:13px}
pre.code::after{content:attr(data-lang);position:absolute;top:8px;right:12px;
  font-family:var(--mono);font-size:10px;letter-spacing:.1em;
  text-transform:uppercase;color:#7d8b87}

.table-wrap{overflow-x:auto;margin:18px 0;border:1px solid var(--line);
  border-radius:11px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:14.5px;line-height:1.7}
th,td{padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#f4f3ef;font-weight:650;font-size:13px;color:var(--muted);
  white-space:nowrap;position:sticky;top:0}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fcfbf9}
.a-right{text-align:right;font-variant-numeric:tabular-nums}
.a-center{text-align:center}

.diagram{margin:24px 0;padding:20px;background:var(--card);
  border:1px solid var(--line);border-radius:13px;overflow-x:auto;text-align:center}
.diagram pre.mermaid{margin:0;font-family:var(--sans)}
.diagram svg{max-width:100%;height:auto}

a.term{color:inherit;text-decoration:none;
  border-bottom:1.5px dotted var(--teal);cursor:help}
a.term:hover{background:var(--teal-soft);border-bottom-style:solid}
a.term code{background:var(--teal-soft)}
a.secref{text-decoration:none;font-weight:600;white-space:nowrap}
a.secref:hover{text-decoration:underline}

.term-strip{margin:26px 0 0;padding:13px 16px;background:var(--card);
  border:1px dashed var(--line);border-radius:11px;display:flex;gap:12px;
  flex-wrap:wrap;align-items:baseline}
.term-strip-label{font-size:11.5px;letter-spacing:.06em;color:var(--muted);
  font-weight:650;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:6px}
a.chip{font-size:12.5px;text-decoration:none;color:var(--teal);
  background:var(--teal-soft);border-radius:20px;padding:2px 11px;line-height:1.7}
a.chip:hover{background:var(--teal);color:#fff}
a.chip code{background:none;color:inherit;padding:0;font-size:12px}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
  gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:13px;
  padding:16px 18px;scroll-margin-top:80px}
.card h4{font-size:15.5px;color:var(--ink);line-height:1.5}
.card h4 code{background:var(--teal-soft);color:var(--teal)}
.card dl{margin:0;font-size:14px;line-height:1.72}
.card .row{display:grid;grid-template-columns:74px minmax(0,1fr);gap:10px;
  padding:6px 0;border-top:1px solid #f0eee9}
.card dt{color:var(--muted);font-size:11.5px;letter-spacing:.04em;
  padding-top:4px;font-weight:650}
.card dd{margin:0}
.card .backrefs dd{font-family:var(--mono);font-size:13px}
.card.flash{animation:flash 1.6s ease-out}
@keyframes flash{0%{box-shadow:0 0 0 3px var(--teal)}100%{box-shadow:0 0 0 0 transparent}}
.card.hidden,.gloss-section.hidden{display:none}

.lede{font-size:15px;color:#39423f;max-width:74ch}
.side-note{font-size:12px;color:var(--muted);margin:0 0 10px;
  font-family:var(--mono)}
.test-part{margin:0 0 54px}
.part-head{display:flex;align-items:baseline;gap:14px;border-top:3px solid var(--teal);
  padding-top:16px}
.test-part[data-part="other"] .part-head{border-top-color:var(--stone)}
.count.big{font-size:13px;background:var(--teal-soft);color:var(--teal);padding:2px 12px}
.test-part[data-part="other"] .count.big{background:var(--stone);color:var(--muted)}
.test-group{margin-bottom:16px}
.group-head{display:flex;align-items:baseline;gap:12px;font-size:16px;
  color:var(--ink);margin:30px 0 4px;scroll-margin-top:74px}
.toc-part{font-weight:650;color:var(--ink)!important;margin-top:8px}
.toc-part .n{color:var(--teal);font-size:9px}
.toc-sub{padding-left:22px!important;font-size:12.5px}
.t-why{width:16%;color:var(--muted);font-size:12.5px}
.group-why{color:var(--muted);font-size:14px;margin:-6px 0 20px;max-width:74ch}
.test-module{margin:26px 0 34px;padding-left:14px;
  border-left:2px solid var(--teal-soft)}
.test-module h3{margin:0 0 4px;display:flex;align-items:baseline;gap:10px;
  font-size:15px}
.test-module h3 code{background:var(--teal-soft);color:var(--teal);font-size:14px}
.test-part[data-part="other"] .test-module{border-left-color:var(--stone)}
.test-part[data-part="other"] .test-module h3 code{background:var(--stone);color:#4a5350}
.suite{margin:16px 0}
.suite h4{font-size:13.5px;color:var(--muted);font-family:var(--mono);
  display:flex;align-items:baseline;gap:9px;margin-bottom:6px}
.count{font-family:var(--mono);font-size:11px;color:var(--muted);
  background:var(--stone);border-radius:20px;padding:1px 8px}
.test-group td{font-size:13.5px;line-height:1.65}
.t-name{width:34%}
.t-name code{background:none;padding:0;color:var(--ink);font-size:12.5px}
.t-how{width:20%;white-space:normal}
.muted{color:var(--muted)}
.derived{color:#6b7472}
.legend{font-size:13.5px;background:var(--card);border:1px solid var(--line);
  border-radius:11px;padding:12px 16px}
.legend b{color:var(--teal)}
.tech{display:inline-block;font-size:11.5px;line-height:1.7;padding:1px 8px;
  margin:1px 3px 1px 0;border-radius:20px;background:var(--stone);
  color:#4a5350;white-space:nowrap}
a.tech-link{text-decoration:none;background:var(--teal-soft);color:var(--teal);
  border-bottom:0;cursor:help}
a.tech-link:hover{background:var(--teal);color:#fff}
.error{color:var(--clay);background:var(--clay-soft);padding:10px 14px;
  border-radius:9px}

#tip{position:fixed;z-index:100;max-width:330px;background:#16201e;color:#f0f2f0;
  font-size:13px;line-height:1.65;padding:9px 12px;border-radius:9px;
  pointer-events:none;opacity:0;transition:opacity .12s;
  box-shadow:0 6px 24px rgba(0,0,0,.22)}
#tip.on{opacity:1}
#tip b{display:block;color:#7fd0c5;font-size:12px;margin-bottom:3px}

#back{position:fixed;left:50%;transform:translateX(-50%);bottom:26px;z-index:60;
  background:var(--ink);color:#fff;border:0;border-radius:22px;padding:10px 20px;
  font:inherit;font-size:13.5px;cursor:pointer;display:none;
  box-shadow:0 6px 20px rgba(0,0,0,.24)}
#back.on{display:block}
#back:hover{background:var(--teal)}

@media print{
  .topbar,.side,#back,#tip{display:none!important}
  .panel{display:block!important} .layout{display:block;max-width:none;padding:0}
  h2{page-break-after:avoid} .diagram,.table-wrap,.card{page-break-inside:avoid}
}
"""

JS = """
const panels = document.querySelectorAll('.panel');
const sides  = document.querySelectorAll('.side');
const tabs   = document.querySelectorAll('.tabs button');

function show(name, push){
  tabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  panels.forEach(p => p.classList.toggle('active', p.dataset.panel === name));
  sides.forEach(s => s.hidden = s.dataset.side !== name);
  if (push) history.replaceState(null, '', '#' + (name === 'doc' ? 'top' : name));
  return name;
}
tabs.forEach(b => b.addEventListener('click', () => {
  show(b.dataset.tab, true);
  window.scrollTo({top: 0});
}));

function panelOf(id){ return document.getElementById(id)?.closest('.panel')?.dataset.panel; }

function jump(id, flash){
  const target = document.getElementById(id);
  if (!target) return false;
  show(panelOf(id) || 'doc', false);
  history.replaceState(null, '', '#' + id);
  target.scrollIntoView({behavior: 'smooth', block: 'start'});
  if (flash){ target.classList.remove('flash'); void target.offsetWidth;
              target.classList.add('flash'); }
  return true;
}

// A term link is a round trip: remember which tab it was clicked from, and
// where on it, so the way back is one click rather than a hunt.
const back = document.getElementById('back');
const TAB_NAMES = {doc: '文件', tests: '測試項目', glossary: '名詞解釋'};
let origin = null;
document.addEventListener('click', ev => {
  const a = ev.target.closest('a[href^="#"]');
  if (!a) return;
  const id = decodeURIComponent(a.getAttribute('href').slice(1));
  const isTerm = ['term', 'chip', 'tech-link'].some(c => a.classList.contains(c));
  const from = document.querySelector('.panel.active')?.dataset.panel;
  if (isTerm) origin = {panel: from, y: window.scrollY};
  if (jump(id, isTerm)){
    ev.preventDefault();
    const crossed = Boolean(isTerm && origin && origin.panel !== panelOf(id));
    back.textContent = '← 回到' + (TAB_NAMES[origin?.panel] ?? '文件');
    back.classList.toggle('on', crossed);
  }
});
back.addEventListener('click', () => {
  if (origin){ show(origin.panel, false); window.scrollTo({top: origin.y}); }
  back.classList.remove('on');
});
window.addEventListener('scroll', () => {
  if (document.querySelector('.panel.active')?.dataset.panel === origin?.panel) {
    back.classList.remove('on');
  }
}, {passive: true});

// Tooltip: the definition, without leaving the sentence you are reading.
const tip = document.getElementById('tip');
document.addEventListener('mouseover', ev => {
  const a = ev.target.closest('a.term, a.tech-link');
  if (!a) return;
  tip.innerHTML = '<b>' + a.dataset.term + '</b>' + a.dataset.tip;
  tip.classList.add('on');
  const r = a.getBoundingClientRect();
  const w = Math.min(330, window.innerWidth - 24);
  tip.style.left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12)) + 'px';
  const below = r.bottom + 10;
  tip.style.top = (below + 120 > window.innerHeight ? r.top - tip.offsetHeight - 10 : below) + 'px';
});
document.addEventListener('mouseout', ev => {
  if (ev.target.closest('a.term, a.tech-link')) tip.classList.remove('on');
});

// Glossary search.
const search = document.getElementById('search');
search?.addEventListener('input', () => {
  const q = search.value.trim().toLowerCase();
  document.querySelectorAll('.gloss-section').forEach(sec => {
    let hits = 0;
    sec.querySelectorAll('.card').forEach(card => {
      const hit = !q || card.dataset.search.toLowerCase().includes(q);
      card.classList.toggle('hidden', !hit);
      if (hit) hits++;
    });
    sec.classList.toggle('hidden', hits === 0);
  });
});

// Test search. Rows hide, then any suite/module/group left with nothing
// hides too -- an empty heading reads as "this group has no such test",
// which is a different claim from "nothing matched".
const testSearch = document.getElementById('test-search');
const testCount = document.getElementById('test-count');
const allRows = document.querySelectorAll('.test-group tbody tr');
function filterTests() {
  const q = (testSearch?.value ?? '').trim().toLowerCase();
  let shown = 0;
  allRows.forEach(row => {
    const hit = !q || row.dataset.search.toLowerCase().includes(q);
    row.hidden = !hit;
    if (hit) shown++;
  });
  document.querySelectorAll('.test-group .suite').forEach(suite => {
    suite.hidden = ![...suite.querySelectorAll('tbody tr')].some(r => !r.hidden);
  });
  document.querySelectorAll('.test-module').forEach(mod => {
    mod.hidden = ![...mod.querySelectorAll('.suite')].some(s => !s.hidden);
  });
  document.querySelectorAll('.test-group').forEach(group => {
    group.hidden = ![...group.querySelectorAll('.test-module')].some(m => !m.hidden);
  });
  document.querySelectorAll('.test-part').forEach(part => {
    part.hidden = ![...part.querySelectorAll('.test-group')].some(g => !g.hidden);
  });
  if (testCount) {
    testCount.textContent = q ? `${shown} / ${allRows.length} 個測試` : `${allRows.length} 個測試`;
  }
}
testSearch?.addEventListener('input', filterTests);
filterTests();

// Which section am I in.
const marks = [...document.querySelectorAll('h2[id]')];
const links = new Map([...document.querySelectorAll('.toc a')]
  .map(a => [decodeURIComponent(a.getAttribute('href').slice(1)), a]));
const spy = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (!e.isIntersecting) return;
    links.forEach(a => a.classList.remove('active'));
    links.get(e.target.id)?.classList.add('active');
  });
}, {rootMargin: '-70px 0px -75% 0px'});
marks.forEach(m => spy.observe(m));

mermaid.initialize({
  startOnLoad: true,
  theme: 'base',
  securityLevel: 'loose',
  fontFamily: getComputedStyle(document.body).fontFamily,
  themeVariables: {
    background: '#ffffff', primaryColor: '#f4f3ef', primaryTextColor: '#1d2422',
    primaryBorderColor: '#c9cdc7', lineColor: '#8a938f', secondaryColor: '#eaece7',
    tertiaryColor: '#faf9f7', clusterBkg: '#faf9f7', clusterBorder: '#d8d6d0',
    fontSize: '14px'
  },
  flowchart: {htmlLabels: true, curve: 'basis', padding: 14},
  sequence: {actorMargin: 42, width: 150, mirrorActors: false}
});

const opening = decodeURIComponent(location.hash.slice(1));
if (['glossary', 'tests'].includes(opening)) show(opening, false);
else if (opening && opening !== 'top') setTimeout(() => jump(opening, true), 60);
"""

PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><span></span>AUTOPERF</div>
  <nav class="tabs">
    <button data-tab="doc" class="active">裝置測試架構</button>
    <button data-tab="tests">測試項目</button>
    <button data-tab="glossary">名詞解釋</button>
  </nav>
  <div class="topmeta">{test_count} 個測試 · {term_count} 個名詞</div>
</header>

<div class="layout">
  <aside class="side" data-side="doc">{doc_toc}</aside>
  <aside class="side" data-side="tests" hidden>
    <input id="test-search" class="search" type="search"
           placeholder="搜尋測試（名稱／目的／方法）…" autocomplete="off">
    <p class="side-note" id="test-count"></p>
    {tests_toc}
  </aside>
  <aside class="side" data-side="glossary" hidden>
    <input id="search" class="search" type="search" placeholder="搜尋名詞…" autocomplete="off">
    {gloss_toc}
  </aside>
  <main>
    <div class="panel active" data-panel="doc">{doc}</div>
    <div class="panel" data-panel="tests">
      <h1>測試項目清單</h1>
      <p class="lede">全部 {test_count} 個測試。
      <strong>先分成「裝置測項」與「非裝置測項」兩大塊</strong>，每一塊裡面再按
      「<strong>什麼情況下會壞</strong>」分組（<a class="secref" href="#sec-4">§四</a> 那個分法，不是按檔案）。
      每一列的目的、方法與裝置判定都是從測試本身抽出來的（<code>scripts/collect-tests.py</code>），
      不是另外手寫的清單：手寫的清單會過期，而過期又看起來完整的清單，
      正是這整套測試在防的那種謊。</p>
      <p class="lede legend"><strong>怎麼判定「裝置測項」。</strong>
      問題是「<strong>如果世界上沒有裝置，這個測試還有意義嗎</strong>」。判定看的是測試本體真的
      碰到什麼 —— 用了哪個 adb 替身、有沒有驅動 adapter、有沒有解析畫面結構或 selector ——
      而不是它放在哪個檔案。每一列右邊的<b>判定依據</b>就是那個證據，所以這個分類可以被反駁。
      刻意<strong>不</strong>算數的：只是存了一個 serial 字串或 scenario 名稱。
      照那樣算，46 個 storage 測試會有 39 個變成「裝置測項」，而 storage 正是這套系統裡
      「沒有裝置也照樣成立」的那一塊。</p>
      <p class="lede legend"><strong>怎麼讀這三欄。</strong>
      <b>測試項目</b>是它在程式碼裡的名字。
      <b>測試目的</b>：有寫 docstring 的就是原文；<span class="derived">淡色的那些</span>
      是從測試名稱直接讀出來的 —— 這個 codebase 的測試名稱本身就是完整的句子，
      所以那不是省略，是它原本就寫在名字裡。
      <b>測試方法</b>是掃描測試本體得到的（用了哪個替身、有沒有 patch、打不打 HTTP），
      點下去會跳到<a href="#glossary">名詞解釋</a>對應的那一則。</p>
      {tests}
    </div>
    <div class="panel" data-panel="glossary">{glossary}</div>
  </main>
</div>

<div id="tip"></div>
<button id="back">← 回到文件</button>

<script>{mermaid}</script>
<script>{js}</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(build())
