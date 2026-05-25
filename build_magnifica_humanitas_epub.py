#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html as html_lib
import re
import shutil
import unicodedata
import uuid
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import yaml
from lxml import etree, html
from PIL import Image, ImageDraw, ImageFont


@dataclass
class Config:
    lang: str
    source_url: str
    source_html: Path
    output_epub: Path
    build_dir: Path
    title: str
    full_title: str
    subtitle: str
    author: str
    publisher: str
    date: str
    # parsing helpers — may differ by language
    intro_anchor: str
    top_level_pattern: str
    # localised UI strings
    cover_label: str
    cover_date: str
    toc_title_page: str
    notes_heading: str

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            lang=data["lang"],
            source_url=data["source_url"],
            source_html=Path(data["source_html"]),
            output_epub=Path(data["output_epub"]),
            build_dir=Path(data["build_dir"]),
            title=data["title"],
            full_title=data["full_title"],
            subtitle=data["subtitle"],
            author=data["author"],
            publisher=data["publisher"],
            date=data["date"],
            intro_anchor=data.get("intro_anchor", "INTRODUCTION_"),
            top_level_pattern=data.get(
                "top_level_pattern",
                r"^(INTRODUCTION|CHAPTER [A-Z]+|CONCLUSION)$",
            ),
            cover_label=data.get("cover_label", "ENCYCLICAL LETTER"),
            cover_date=data.get("cover_date", ""),
            toc_title_page=data.get("toc_title_page", "Title Page"),
            notes_heading=data.get("notes_heading", "Notes"),
        )


def normspace(value: str) -> str:
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def escape(value: str) -> str:
    return html_lib.escape(value, quote=True)


def safe_id(value: str, used: set[str]) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", normalized).strip("_")
    normalized = normalized or "section"
    if normalized[0].isdigit():
        normalized = f"id_{normalized}"

    candidate = normalized
    i = 2
    while candidate in used:
        candidate = f"{normalized}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def any_ancestor_tag(element: etree._Element, tag_name: str) -> bool:
    return any(isinstance(ancestor.tag, str) and ancestor.tag.lower() == tag_name for ancestor in element.iterancestors())


def remove_preserving_tail(parent: etree._Element, child: etree._Element) -> None:
    tail = child.tail
    previous = child.getprevious()
    parent.remove(child)
    if tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail


def build_id_map(content: etree._Element) -> dict[str, str]:
    used: set[str] = set()
    ids: dict[str, str] = {}
    for anchor in content.xpath(".//a[@name]"):
        old = anchor.get("name")
        if old and old not in ids:
            ids[old] = safe_id(old, used)
    return ids


def extract_toc_levels(content: etree._Element, id_map: dict[str, str], cfg: Config) -> dict[str, int]:
    levels: dict[str, int] = {}
    top_re = re.compile(cfg.top_level_pattern)

    for paragraph in content.xpath("./p"):
        if paragraph.xpath(f'.//a[@name="{cfg.intro_anchor}"]'):
            break

        for anchor in paragraph.xpath('.//a[@href and starts-with(@href, "#")]'):
            href = anchor.get("href", "")
            old_id = href[1:]
            if old_id.startswith("_ftn") or old_id not in id_map:
                continue

            text = normspace(anchor.text_content())
            if not text:
                continue

            if top_re.match(text):
                level = 1
            elif any_ancestor_tag(anchor, "i"):
                level = 3
            else:
                level = 2

            levels.setdefault(id_map[old_id], level)
    return levels


def sanitize_element(element: etree._Element, id_map: dict[str, str], source_url: str) -> etree._Element:
    cleaned = deepcopy(element)

    def clean(node: etree._Element) -> None:
        if not isinstance(node.tag, str):
            return

        tag = node.tag.lower()
        if tag == "b":
            node.tag = "strong"
        elif tag == "i":
            node.tag = "em"

        if node.text:
            node.text = node.text.replace("\xa0", " ")
        if node.tail:
            node.tail = node.tail.replace("\xa0", " ")

        for child in list(node):
            if not isinstance(child.tag, str):
                remove_preserving_tail(node, child)
                continue
            clean(child)

        original_style = node.get("style", "")
        name = node.get("name")
        href = node.get("href")
        text_align_center = "text-align: center" in original_style.lower()

        for attr in list(node.attrib):
            del node.attrib[attr]

        if name:
            node.set("id", id_map.get(name, name))

        if node.tag == "a" and href:
            if href.startswith("#"):
                node.set("href", f"#{id_map.get(href[1:], href[1:])}")
            else:
                node.set("href", urljoin(source_url, href).replace("http://www.vatican.va", "https://www.vatican.va"))

        if text_align_center and node.tag in {"p", "div"}:
            node.set("class", "center")

    clean(cleaned)
    return cleaned


def heading_from_paragraph(
    paragraph: etree._Element,
    id_map: dict[str, str],
    toc_levels: dict[str, int],
) -> tuple[etree._Element | None, dict[str, str | int] | None]:
    anchors = paragraph.xpath('.//a[@name and not(starts-with(@name, "_ftn"))]')
    if not anchors:
        return None, None

    old_id = anchors[0].get("name")
    if not old_id:
        return None, None

    new_id = id_map.get(old_id, old_id)
    text = normspace(paragraph.text_content())
    if not text or len(text) > 220:
        return None, None

    is_heading = bool(paragraph.xpath(".//b")) or "text-align: center" in paragraph.get("style", "").lower()
    if not is_heading:
        return None, None

    level = toc_levels.get(new_id, 2)
    tag = "h1" if level <= 1 else "h2" if level == 2 else "h3"
    heading = etree.Element(tag)
    heading.set("id", new_id)
    heading.text = text
    return heading, {"id": new_id, "text": text, "level": level}


def serialize_element(element: etree._Element) -> str:
    return etree.tostring(element, encoding="unicode", method="xml", with_tail=False)


def build_content_fragments(
    content: etree._Element,
    id_map: dict[str, str],
    toc_levels: dict[str, int],
    cfg: Config,
) -> tuple[list[str], list[str], list[dict[str, str | int]]]:
    children = list(content)
    start_index = None
    for i, child in enumerate(children):
        if isinstance(child.tag, str) and child.xpath(f'.//a[@name="{cfg.intro_anchor}"]'):
            start_index = i
            break
    if start_index is None:
        raise RuntimeError(
            f"Could not find the start of the encyclical body (anchor '{cfg.intro_anchor}'). "
            "Check intro_anchor in your config."
        )

    body: list[str] = []
    notes: list[str] = []
    toc_entries: list[dict[str, str | int]] = []

    for child in children[start_index:]:
        if not isinstance(child.tag, str):
            continue

        footnote_paragraphs = child.xpath('.//p[contains(concat(" ", normalize-space(@class), " "), " MsoFootnoteText ")]')
        if footnote_paragraphs:
            for paragraph in footnote_paragraphs:
                cleaned = sanitize_element(paragraph, id_map, cfg.source_url)
                cleaned.set("class", "footnote")
                if normspace(cleaned.text_content()):
                    notes.append(serialize_element(cleaned))
            continue

        if child.tag.lower() == "hr":
            continue

        if child.tag.lower() == "p" and not normspace(child.text_content()):
            continue

        heading, toc_entry = heading_from_paragraph(child, id_map, toc_levels)
        if heading is not None and toc_entry is not None:
            body.append(serialize_element(heading))
            if not str(toc_entry["id"]).startswith("ftn"):
                toc_entries.append(toc_entry)
            continue

        cleaned = sanitize_element(child, id_map, cfg.source_url)
        if cleaned.tag.lower() == "p" and not normspace(cleaned.text_content()):
            continue
        body.append(serialize_element(cleaned))

    return body, notes, toc_entries


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSerif.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font_obj: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font_obj)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_centered_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font_obj: ImageFont.ImageFont,
    y: int,
    fill: str,
    line_gap: int,
    width: int,
) -> int:
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font_obj)
        x = (width - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=font_obj, fill=fill)
        y += (bbox[3] - bbox[1]) + line_gap
    return y


def create_cover(path: Path, cfg: Config) -> None:
    # 800×1200 (960 000 px) fits within the crosspoint-reader firmware limit of
    # MAX_SOURCE_PIXELS = 3 145 728 (2048×1536). The original 1600×2400 = 3 840 000
    # exceeds that limit and causes the decoder to silently reject the image.
    width, height = 800, 1200
    image = Image.new("RGB", (width, height), "#f7f1e6")
    draw = ImageDraw.Draw(image)
    burgundy = "#6d1726"
    graphite = "#262626"
    gold = "#b9965d"

    margin = 55
    draw.rectangle((margin, margin, width - margin, height - margin), outline=burgundy, width=4)
    draw.rectangle((margin + 16, margin + 16, width - margin - 16, height - margin - 16), outline=gold, width=2)

    y = 180
    y = draw_centered_lines(draw, [cfg.cover_label.upper()], font(27), y, graphite, 9, width)
    y += 60
    y = draw_centered_lines(draw, ["MAGNIFICA", "HUMANITAS"], font(63, bold=True), y, burgundy, 17, width)
    y += 60
    subtitle_lines = wrap_text(draw, cfg.subtitle.upper(), font(24), width - 180)
    y = draw_centered_lines(draw, subtitle_lines, font(24), y, graphite, 11, width)
    y += 95
    draw.line((width // 2 - 105, y, width // 2 + 105, y), fill=gold, width=3)
    y += 55
    y = draw_centered_lines(draw, [cfg.author.upper()], font(29, bold=True), y, graphite, 9, width)
    y += 24
    draw_centered_lines(draw, [cfg.cover_date.upper()], font(22), y, graphite, 7, width)

    image.save(path, "PNG", optimize=True)


def make_xhtml(title: str, body: str, lang: str, css_href: str = "../styles/book.css") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
<head>
  <meta charset="utf-8" />
  <title>{escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="{escape(css_href)}" />
</head>
<body>
{body}
</body>
</html>
"""


def render_nav_tree(entries: list[dict[str, str | int]]) -> str:
    root: list[dict[str, object]] = []
    stack: list[tuple[int, list[dict[str, object]]]] = [(0, root)]

    for entry in entries:
        level = int(entry.get("level", 1))
        level = max(1, min(level, 3))
        while stack and level <= stack[-1][0]:
            stack.pop()
        node: dict[str, object] = {
            "href": entry["href"],
            "text": entry["text"],
            "children": [],
        }
        stack[-1][1].append(node)
        stack.append((level, node["children"]))  # type: ignore[arg-type]

    def render(nodes: list[dict[str, object]]) -> str:
        items = ["<ol>"]
        for node in nodes:
            items.append(f'<li><a href="{escape(str(node["href"]))}">{escape(str(node["text"]))}</a>')
            children = node["children"]
            if children:
                items.append(render(children))  # type: ignore[arg-type]
            items.append("</li>")
        items.append("</ol>")
        return "\n".join(items)

    return render(root)


def write_epub_files(body_fragments: list[str], notes: list[str], toc_entries: list[dict[str, str | int]], cfg: Config) -> None:
    if cfg.build_dir.exists():
        shutil.rmtree(cfg.build_dir)

    (cfg.build_dir / "META-INF").mkdir(parents=True)
    (cfg.build_dir / "OEBPS" / "text").mkdir(parents=True)
    (cfg.build_dir / "OEBPS" / "styles").mkdir(parents=True)
    (cfg.build_dir / "OEBPS" / "images").mkdir(parents=True)

    (cfg.build_dir / "mimetype").write_text("application/epub+zip", encoding="utf-8")
    (cfg.build_dir / "META-INF" / "container.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml" />
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )

    create_cover(cfg.build_dir / "OEBPS" / "images" / "cover.png", cfg)

    css = """
body {
  font-family: Georgia, "Times New Roman", serif;
  line-height: 1.45;
  margin: 5%;
  color: #1f1f1f;
}
a {
  color: #6d1726;
  text-decoration: none;
}
h1, h2, h3 {
  font-family: Georgia, "Times New Roman", serif;
  color: #5f1320;
  line-height: 1.2;
  margin-top: 1.6em;
}
h1 {
  font-size: 1.7em;
  text-align: center;
}
h2 {
  font-size: 1.3em;
}
h3 {
  font-size: 1.08em;
  font-style: italic;
}
p {
  margin: 0 0 0.85em;
  text-align: justify;
}
.center {
  text-align: center;
}
.title-page {
  text-align: center;
  margin-top: 18%;
}
.title-page p {
  text-align: center;
}
.book-title {
  font-size: 2.2em;
  color: #5f1320;
  margin: 0.6em 0 0.2em;
}
.subtitle {
  font-size: 1.1em;
}
.source {
  font-size: 0.85em;
  margin-top: 3em;
}
.footnote {
  font-size: 0.88em;
  text-align: left;
}
#notes {
  border-top: 1px solid #b9965d;
  margin-top: 2em;
  padding-top: 1em;
}
img.cover {
  display: block;
  height: auto;
  margin: 0 auto;
  max-width: 100%;
}
""".strip()
    (cfg.build_dir / "OEBPS" / "styles" / "book.css").write_text(css + "\n", encoding="utf-8")

    cover_body = f'<section class="cover"><img class="cover" src="../images/cover.png" alt="Cover for {escape(cfg.title)}" /></section>'
    (cfg.build_dir / "OEBPS" / "text" / "cover.xhtml").write_text(
        make_xhtml("Cover", cover_body, cfg.lang), encoding="utf-8"
    )

    title_body = f"""
<section class="title-page">
  <p>{escape(cfg.cover_label)}</p>
  <h1 class="book-title">{escape(cfg.title)}</h1>
  <p class="subtitle">{escape(cfg.subtitle)}</p>
  <p>{escape(cfg.author)}</p>
  <p>{escape(cfg.date)}</p>
  <p class="source">Source: <a href="{escape(cfg.source_url)}">{escape(cfg.publisher)}</a></p>
</section>
""".strip()
    (cfg.build_dir / "OEBPS" / "text" / "title.xhtml").write_text(
        make_xhtml(cfg.title, title_body, cfg.lang), encoding="utf-8"
    )

    notes_section = ""
    if notes:
        notes_section = f"""
<section id="notes" epub:type="footnotes">
  <h1>{escape(cfg.notes_heading)}</h1>
  {"\n  ".join(notes)}
</section>
""".strip()

    content_body = f"""
<section id="encyclical">
  {"\n  ".join(body_fragments)}
</section>
{notes_section}
""".strip()
    (cfg.build_dir / "OEBPS" / "text" / "content.xhtml").write_text(
        make_xhtml(cfg.title, content_body, cfg.lang), encoding="utf-8"
    )

    nav_entries = [
        {"href": "text/title.xhtml", "text": cfg.toc_title_page, "level": 1},
        *[
            {
                "href": f'text/content.xhtml#{entry["id"]}',
                "text": str(entry["text"]),
                "level": int(entry["level"]),
            }
            for entry in toc_entries
        ],
    ]
    if notes:
        nav_entries.append({"href": "text/content.xhtml#notes", "text": cfg.notes_heading, "level": 1})

    nav_body = f"""
<nav epub:type="toc" id="toc">
  <h1>Contents</h1>
  {render_nav_tree(nav_entries)}
</nav>
""".strip()
    (cfg.build_dir / "OEBPS" / "nav.xhtml").write_text(
        make_xhtml("Contents", nav_body, cfg.lang, "styles/book.css"), encoding="utf-8"
    )

    ncx_points = []
    play_order = 1
    for entry in nav_entries:
        ncx_points.append(
            f"""    <navPoint id="navPoint-{play_order}" playOrder="{play_order}">
      <navLabel><text>{escape(str(entry["text"]))}</text></navLabel>
      <content src="{escape(str(entry["href"]))}" />
    </navPoint>"""
        )
        play_order += 1

    identifier = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, cfg.source_url)}"
    (cfg.build_dir / "OEBPS" / "toc.ncx").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{identifier}" />
    <meta name="dtb:depth" content="3" />
    <meta name="dtb:totalPageCount" content="0" />
    <meta name="dtb:maxPageNumber" content="0" />
  </head>
  <docTitle><text>{escape(cfg.title)}</text></docTitle>
  <docAuthor><text>{escape(cfg.author)}</text></docAuthor>
  <navMap>
{"\n".join(ncx_points)}
  </navMap>
</ncx>
""",
        encoding="utf-8",
    )

    (cfg.build_dir / "OEBPS" / "content.opf").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{cfg.lang}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{identifier}</dc:identifier>
    <dc:title>{escape(cfg.title)}</dc:title>
    <dc:creator>{escape(cfg.author)}</dc:creator>
    <dc:language>{cfg.lang}</dc:language>
    <dc:publisher>{escape(cfg.publisher)}</dc:publisher>
    <dc:date>{cfg.date}</dc:date>
    <dc:source>{escape(cfg.source_url)}</dc:source>
    <dc:description>{escape(cfg.subtitle)}</dc:description>
    <meta property="dcterms:modified">2026-05-25T00:00:00Z</meta>
    <meta name="cover" content="cover-image" />
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />
    <item id="style" href="styles/book.css" media-type="text/css" />
    <item id="cover-image" href="images/cover.png" media-type="image/png" properties="cover-image" />
    <item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml" />
    <item id="title-page" href="text/title.xhtml" media-type="application/xhtml+xml" />
    <item id="content" href="text/content.xhtml" media-type="application/xhtml+xml" />
  </manifest>
  <spine toc="ncx">
    <itemref idref="cover" />
    <itemref idref="title-page" />
    <itemref idref="content" />
  </spine>
</package>
""",
        encoding="utf-8",
    )


def create_epub(cfg: Config) -> None:
    if cfg.output_epub.exists():
        cfg.output_epub.unlink()

    with zipfile.ZipFile(cfg.output_epub, "w") as epub:
        epub.write(cfg.build_dir / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in sorted(cfg.build_dir.rglob("*")):
            if path.is_dir() or path.name == "mimetype":
                continue
            epub.write(path, path.relative_to(cfg.build_dir).as_posix(), compress_type=zipfile.ZIP_DEFLATED)


def validate_xml(cfg: Config) -> None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    for path in [
        cfg.build_dir / "META-INF" / "container.xml",
        cfg.build_dir / "OEBPS" / "content.opf",
        cfg.build_dir / "OEBPS" / "toc.ncx",
        cfg.build_dir / "OEBPS" / "nav.xhtml",
        cfg.build_dir / "OEBPS" / "text" / "cover.xhtml",
        cfg.build_dir / "OEBPS" / "text" / "title.xhtml",
        cfg.build_dir / "OEBPS" / "text" / "content.xhtml",
    ]:
        etree.parse(str(path), parser)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an EPUB from a Vatican encyclical page.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("en.yaml"),
        help="Path to language config YAML (default: en.yaml)",
    )
    args = parser.parse_args()

    cfg = Config.from_yaml(args.config)

    if not cfg.source_html.exists():
        print(f"Downloading {cfg.source_url} ...")
        request = Request(cfg.source_url, headers={"User-Agent": "magnifica-humanitas-epub/1.0"})
        with urlopen(request, timeout=30) as response:
            cfg.source_html.write_bytes(response.read())

    document = html.parse(str(cfg.source_html))
    root = document.getroot()
    content_nodes = root.xpath(
        '//*[contains(concat(" ", normalize-space(@class), " "), " documento ")]'
        '//*[contains(concat(" ", normalize-space(@class), " "), " vaticanrichtext ") '
        'and contains(concat(" ", normalize-space(@class), " "), " text ")][2]'
    )
    if not content_nodes:
        raise RuntimeError("Could not find the Vatican document text.")

    content = content_nodes[0]
    id_map = build_id_map(content)
    toc_levels = extract_toc_levels(content, id_map, cfg)
    body_fragments, notes, toc_entries = build_content_fragments(content, id_map, toc_levels, cfg)

    write_epub_files(body_fragments, notes, toc_entries, cfg)
    validate_xml(cfg)
    create_epub(cfg)

    print(f"Wrote {cfg.output_epub}")
    print(f"Body fragments: {len(body_fragments)}")
    print(f"Notes: {len(notes)}")
    print(f"TOC entries: {len(toc_entries)}")


if __name__ == "__main__":
    main()
