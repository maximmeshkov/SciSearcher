"""Extract searchable text without changing the original article file."""

from collections.abc import Iterator
from dataclasses import dataclass, field
import hashlib
from io import BytesIO
from pathlib import PurePosixPath
import re
import subprocess
from xml.etree import ElementTree as ET


# Limits of the pypdf fallback, used only when MinerU is off.
MAX_PDF_PAGES = 500
MAX_PDF_PAGE_STREAM_BYTES = 8 * 1024 * 1024
MAX_XML_ELEMENTS = 150_000
MAX_XML_DEPTH = 80

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")
_PAGE_MARKER = re.compile(r"^## Страница ([1-9][0-9]{0,5})[ \t]*$")
# Page marker of prepared text: invisible in rendered Markdown and keeps section headings across pages.
_PAGE_COMMENT = re.compile(r"^<!-- page: ([1-9][0-9]{0,5}) -->$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+)$")
_FENCE = re.compile(r"^[ ]{0,3}(\x60{3,}|~{3,})")
_SECTIONS = frozenset({"sec", "div", "div1", "div2", "div3", "div4", "div5", "div6", "div7"})
_SKIP = frozenset({"back", "ref-list", "listbibl", "bibliography", "references", "graphic", "media", "svg"})
_EMBEDDED_BLOCKS = frozenset({"table-wrap", "fig", "figure", "table", "list"})
_PARAGRAPHS = frozenset({"p", "ab", "line", "l", "disp-quote", "quote", "preformat", "code"})
_CAPTIONS = frozenset({"caption", "figdesc", "label", "head", "title"})


def _clean_text(text: str) -> str:
    text = _CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _has_text(text: str) -> bool:
    return any(character.isalnum() for character in text)


def _extract_pdf(data: bytes) -> tuple[str, int]:
    if not data.startswith(b"%PDF-"):
        raise ValueError("Файл не распознан как PDF.")

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("Для разбора PDF требуется доступная библиотека pypdf.") from exc

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF защищён паролем. Импортируйте версию, которую можно открыть.")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f"В PDF больше {MAX_PDF_PAGES} страниц.")
        sections = []
        for page_number, page in enumerate(reader.pages, start=1):
            contents = page.get_contents()
            if contents is None:
                continue
            if len(contents.get_data()) > MAX_PDF_PAGE_STREAM_BYTES:
                raise ValueError(f"Слишком большой поток содержимого PDF на странице {page_number}.")
            text = _clean_text(page.extract_text() or "")
            if _has_text(text):
                sections.append(f"## Страница {page_number}\n\n{text}")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Не удалось прочитать PDF. Проверьте, открывается ли файл.") from exc

    if not sections:
        raise ValueError("В PDF нет извлекаемого текста. Для скана сначала нужен OCR.")
    return "\n\n".join(sections), page_count


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold()


def _xml_root(data: bytes) -> ET.Element:
    # External DTD references are common in published JATS files. Keep them
    # inert while rejecting custom entity declarations, including UTF-16 XML.
    if b"<!ENTITY" in data.replace(b"\x00", b""):
        raise ValueError("Пользовательские XML-сущности не поддерживаются.")
    try:
        parser = ET.iterparse(BytesIO(data), events=("start", "end"))
        count = depth = 0
        for event, _ in parser:
            if event == "start":
                depth += 1
                count += 1
                if depth > MAX_XML_DEPTH or count > MAX_XML_ELEMENTS:
                    raise ValueError("XML превышает допустимую сложность.")
            else:
                depth -= 1
        return parser.root
    except (ET.ParseError, LookupError, UnicodeError) as exc:
        raise ValueError("Не удалось прочитать XML. Нужен корректный одиночный JATS или TEI.") from exc


def _inline(element: ET.Element) -> str:
    name = _tag(element)
    if name in _SKIP or name in _EMBEDDED_BLOCKS:
        return ""
    if name in {"formula", "inline-formula", "disp-formula"}:
        latex = next((part for part in element.iter() if _tag(part) == "tex-math"), None)
        if latex is not None:
            return "".join(latex.itertext())
    if name in {"break", "br", "lb"}:
        return " "
    parts = [element.text or ""]
    for child in element:
        parts.extend((_inline(child), child.tail or ""))
    return "".join(parts)


def _one_line(element: ET.Element) -> str:
    return " ".join(_inline(element).split())


def _table_lines(element: ET.Element) -> Iterator[str]:
    rows = []
    header_index = None
    for row in element.iter():
        if _tag(row) not in {"tr", "row"}:
            continue
        cells = [cell for cell in row if _tag(cell) in {"th", "td", "cell", "entry"}]
        if not cells:
            continue
        if not rows and (any(_tag(cell) == "th" for cell in cells) or row.get("role") == "label"):
            header_index = len(rows)
        rows.append([_one_line(cell).replace("|", r"\|") for cell in cells])
    if not rows:
        return
    table_lines = []
    for index, row in enumerate(rows):
        table_lines.append("| " + " | ".join(row) + " |")
        if index == header_index:
            table_lines.append("| " + " | ".join("---" for _ in row) + " |")
    yield "\n".join(table_lines)


def _xml_blocks(element: ET.Element, level: int = 2) -> Iterator[str]:
    name = _tag(element)
    role = element.get("sec-type", element.get("type", "")).casefold()
    if name in _SKIP or role in {"references", "bibliography"}:
        return
    if name == "table":
        yield from _table_lines(element)
        return
    if name in _PARAGRAPHS:
        text = _one_line(element)
        if text:
            yield text
        # Figures and tables are also permitted inside a JATS paragraph.
        for child in element:
            if _tag(child) in _EMBEDDED_BLOCKS:
                yield from _xml_blocks(child, level)
        return
    if name in _CAPTIONS or name in {"item", "list-item", "formula", "disp-formula"}:
        text = _one_line(element)
        if text:
            yield f"- {text}" if name in {"item", "list-item"} else text
        return
    if element.text and element.text.strip():
        yield " ".join(element.text.split())
    for child in element:
        if name in _SECTIONS and _tag(child) in {"title", "head"}:
            title = _one_line(child)
            if title:
                yield f"{'#' * min(level, 6)} {title}"
        else:
            next_level = level + int(name in _SECTIONS and _tag(child) in _SECTIONS)
            yield from _xml_blocks(child, next_level)
        if child.tail and child.tail.strip():
            yield " ".join(child.tail.split())


def _extract_xml(data: bytes) -> str:
    root = _xml_root(data)
    if _tag(root) in {"pmc-articleset", "article-set", "teicorpus"}:
        documents = [element for element in root if _tag(element) in {"article", "tei"}]
        if len(documents) != 1:
            raise ValueError("Импортируйте один документ JATS или TEI на статью.")
        root = documents[0]
    if _tag(root) not in {"article", "tei"}:
        raise ValueError("Поддерживается научный XML в формате JATS или TEI.")

    container = root
    if _tag(root) == "tei":
        container = next((part for part in root if _tag(part) == "text"), None)
        if container is None:
            raise ValueError("В XML TEI нет основного текста.")
    body = next((part for part in container if _tag(part) == "body"), None)
    if body is None:
        raise ValueError("В XML нет основного текста статьи.")
    body_text = "\n\n".join(_xml_blocks(body))
    if not any(_has_text(part["text"]) for part in chunks(body_text)):
        raise ValueError("Основной текст XML пуст.")

    headers = [element for element in root if _tag(element) in {"front", "teiheader"}]
    if container is not root:
        headers.extend(element for element in container if _tag(element) == "front")
    title = next(
        (_one_line(part) for header in headers for part in header.iter()
         if _tag(part) == "article-title"),
        "",
    )
    if not title and _tag(root) == "tei":
        title = next(
            (_one_line(part) for header in headers for group in header.iter()
             if _tag(group) == "titlestmt" for part in group if _tag(part) == "title"),
            "",
        )
    abstract = next(
        (part for header in headers for part in header.iter() if _tag(part) == "abstract"),
        None,
    )
    blocks = [f"# {title}"] if title else []
    if abstract is not None:
        abstract_text = "\n\n".join(_xml_blocks(abstract))
        if _has_text(abstract_text):
            blocks.append(f"## Аннотация\n\n{abstract_text}")
            if not _HEADING.fullmatch(body_text.split("\n", 1)[0]):
                blocks.append("## Основной текст")
    blocks.append(body_text)
    return "\n\n".join(blocks)


def extract(data: bytes, format: str) -> tuple[str, int]:
    """Return text and the original PDF page count (zero for other formats)."""
    if not data:
        raise ValueError("Файл пуст.")
    kind = format.casefold().lstrip(".")
    if kind == "pdf":
        return _extract_pdf(data)
    if kind == "xml":
        text = _extract_xml(data)
    elif kind in {"md", "txt"}:
        try:
            text = _clean_text(data.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise ValueError("Markdown и TXT нужно сохранять в UTF-8.") from exc
        if not _has_text(text):
            raise ValueError("В файле нет читаемого текста.")
    else:
        raise ValueError("Поддерживаются PDF, XML (JATS/TEI), Markdown и TXT.")
    return text, 0


def has_signature(format_name: str, head: bytes) -> bool:
    """Whether the first bytes of a file match its format; text formats have no signature."""
    if format_name == "pdf":
        return b"%PDF-" in head[:1024]
    if format_name == "djvu":
        return head.startswith(b"AT&TFORM")
    return True


def djvu_to_pdf(source, target):
    """Render a DjVu file to PDF with ddjvu (DjVuLibre) so MinerU can read its pages; returns target."""
    try:
        subprocess.run(["ddjvu", "-format=pdf", str(source), str(target)], check=True, capture_output=True)
    except FileNotFoundError:
        raise ValueError("Для DjVu нужна утилита ddjvu из пакета djvulibre-bin.") from None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace").strip()[-300:]
        raise ValueError(f"ddjvu не смог перевести DjVu в PDF: {detail}") from None
    return target


@dataclass(frozen=True)
class PreparedText:
    """Article text ready for agents: Markdown body, the reference list kept apart, page count,
    and the pictures of figures and tables the body links to, by digest."""

    body: str
    references: str
    pages: int
    figures: dict[str, bytes] = field(default_factory=dict)


_MINERU_SKIPPED = frozenset({"header", "footer", "page_number"})
_MINERU_HEADINGS = {"doc_title": "#", "paragraph_title": "##"}
_MINERU_FIGURES = frozenset({"table", "chart", "image"})
FIGURE_URL = "/api/files/"  # where the service serves stored pictures


def _mineru_spans(content) -> str:
    """Flatten MinerU spans to text; inline formulas become $…$."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_mineru_spans(part) for part in content)
    if isinstance(content, dict):
        text = _mineru_spans(content.get("content", "")).strip()
        return f"${text}$" if content.get("type") == "equation_inline" and text else text
    return ""


def _mineru_block(block: dict, links: dict[str, str]) -> str:
    """Render one MinerU block as Markdown.

    Figures and tables become a link to their picture plus the caption; tables also keep their HTML.
    `links` maps MinerU's image paths to the URLs of the stored pictures.
    """
    kind = block.get("type")
    content = block.get("content", "")
    if kind == "equation":
        latex = _mineru_spans(content).strip()
        return f"$$\n{latex}\n$$" if latex else ""
    if kind in _MINERU_FIGURES and isinstance(content, list):
        parts = []
        for part in content:
            part_kind = part.get("type", "") if isinstance(part, dict) else ""
            if part_kind.endswith("_body"):
                if part.get("image_path") in links:
                    parts.append(f"![{kind}]({links[part['image_path']]})")
                if part_kind == "table_body":
                    parts.append(_mineru_spans(part.get("content", "")).strip())
            else:
                parts.append(" ".join(_mineru_spans(part).split()))
        return "\n\n".join(filter(None, parts))
    text = " ".join(_mineru_spans(content).split())
    return f"{_MINERU_HEADINGS[kind]} {text}" if text and kind in _MINERU_HEADINGS else text


def from_mineru(middle: dict, images: dict[str, bytes] | None = None) -> PreparedText:
    """Build prepared text from MinerU middle JSON: page markers, headings, LaTeX, tables, captions,
    and links to the pictures of figures and tables (`images`: MinerU image path → bytes).

    Running headers, footers and page numbers are dropped; references are returned separately.
    """
    pages = middle.get("pages") if isinstance(middle, dict) else None
    if not isinstance(pages, list) or not pages:
        raise ValueError("MinerU вернул разбор без страниц.")
    figures = {name: hashlib.sha256(data).hexdigest() for name, data in (images or {}).items()}
    links = {name: FIGURE_URL + digest + PurePosixPath(name).suffix.lower() for name, digest in figures.items()}
    body, references = [], []
    for page in pages:
        body.append(f"<!-- page: {int(page.get('page_idx', 0)) + 1} -->")
        for block in page.get("blocks") or []:
            if not isinstance(block, dict) or block.get("type") in _MINERU_SKIPPED:
                continue
            text = _clean_text(_mineru_block(block, links))
            if not text:
                continue
            (references if block.get("type") == "ref_text" else body).append(text)
    if not any(_has_text(part) for part in body if not _PAGE_COMMENT.fullmatch(part)):
        raise ValueError("MinerU не нашёл в PDF текста. Для скана нужен уровень разбора с OCR.")
    return PreparedText("\n\n".join(body), "\n".join(references), len(pages),
                        {figures[name]: data for name, data in (images or {}).items()})


def _chunk_section(text: str, target: int, overlap: int) -> Iterator[str]:
    start = 0
    while start < len(text):
        end = min(start + target, len(text))
        if end < len(text):
            lower = start + max(target // 2, overlap + 1)
            for separator in ("\n\n", "\n", " "):
                boundary = text.rfind(separator, lower, end)
                if boundary >= lower:
                    end = boundary
                    break
        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        if end == len(text):
            break
        start = end - overlap
        # Avoid starting on a half-word where a nearby whole word is available.
        while start < end and start > 0 and not text[start - 1].isspace() and not text[start].isspace():
            start += 1


def chunks(text: str, target: int = 1800, overlap: int = 180) -> Iterator[dict]:
    """Yield AI search chunks without crossing page or Markdown section boundaries."""
    if target <= 0 or not 0 <= overlap < target:
        raise ValueError("Перекрытие фрагментов должно быть меньше их размера.")
    headings: list[tuple[int, str]] = []
    page = None
    lines = []
    fence = None
    yielded = False

    def flush() -> Iterator[dict]:
        nonlocal yielded
        context = {"page": page} if page is not None else {}
        if headings:
            context["section"] = " / ".join(title for _, title in headings)
        for part in _chunk_section("\n".join(lines).strip(), target, overlap):
            yielded = True
            yield {"text": part, **context}

    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append(line)
            continue
        page_marker = _PAGE_MARKER.fullmatch(line) if fence is None else None
        page_comment = _PAGE_COMMENT.fullmatch(line) if fence is None else None
        heading = _HEADING.fullmatch(line) if fence is None else None
        if not (page_marker or page_comment or heading):
            lines.append(line)
            continue
        yield from flush()
        lines.clear()
        if page_marker:
            page = int(page_marker[1])
            headings.clear()
        elif page_comment:
            page = int(page_comment[1])
        elif heading:
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading[2].strip()))
    yield from flush()
    if not yielded:
        for part in _chunk_section(text.strip(), target, overlap):
            yield {"text": part}
