"""Extract searchable text without changing the original article file."""

from collections.abc import Iterator
from io import BytesIO
import re
from xml.etree import ElementTree as ET


MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_PDF_PAGES = 500
MAX_PDF_PAGE_STREAM_BYTES = 8 * 1024 * 1024
MAX_XML_ELEMENTS = 150_000
MAX_XML_DEPTH = 80

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")
_PAGE_MARKER = re.compile(r"^## Страница ([1-9][0-9]{0,5})[ \t]*$")
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
        total_chars = 0
        for page_number, page in enumerate(reader.pages, start=1):
            contents = page.get_contents()
            if contents is None:
                continue
            if len(contents.get_data()) > MAX_PDF_PAGE_STREAM_BYTES:
                raise ValueError(f"Слишком большой поток содержимого PDF на странице {page_number}.")
            text = _clean_text(page.extract_text() or "")
            if _has_text(text):
                section = f"## Страница {page_number}\n\n{text}"
                total_chars += len(section) + 2
                if total_chars > MAX_TEXT_CHARS:
                    raise ValueError("Извлечённый текст PDF превышает допустимый размер.")
                sections.append(section)
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
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Файл пуст или больше 32 МиБ.")
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
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("Извлечённый текст превышает допустимый размер.")
    return text, 0


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

    def flush() -> Iterator[dict]:
        context = {"page": page} if page is not None else {}
        if headings:
            context["section"] = " / ".join(title for _, title in headings)
        for part in _chunk_section("\n".join(lines).strip(), target, overlap):
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
        heading = _HEADING.fullmatch(line) if fence is None else None
        if not (page_marker or heading):
            lines.append(line)
            continue
        yield from flush()
        lines.clear()
        if page_marker:
            page = int(page_marker[1])
            headings.clear()
        elif heading:
            level = len(heading[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, heading[2].strip()))
    yield from flush()
