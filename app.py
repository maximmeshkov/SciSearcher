import argparse
import base64
import binascii
import hmac
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from catalog import Catalog, content_kind, normalize, normalize_doi
from documents import chunks, djvu_to_pdf, extract, from_mineru, has_signature, pdf_page_png
from discovery import Discovery
from mineru import MinerU
from rag import Rag, RagUnavailable
from sources import allows_abstract_export, fetch_pages, get_bytes, pdf_error, resolved_pdf, source_specs


STATIC = Path(__file__).resolve().parent / "static"
PAGE_SIZE = 500
DOWNLOAD_LIMIT = 40
# Only downloads from the internet are limited: a server must not be able to fill the disk.
MAX_DOWNLOAD_BYTES = int(os.environ.get("MAX_DOWNLOAD_MB", "200")) * 1024 * 1024
PARSED_FORMATS = ("pdf", "djvu")  # the article's own document; MinerU turns it into text
UPLOAD_FORMATS = {"pdf": "pdf", "djvu": "djvu", "djv": "djvu", "xml": "xml", "jats": "xml",
                  "md": "md", "markdown": "md", "txt": "txt"}
PICTURE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


def web_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
        return ""
    try:
        url = urlsplit(value)
        if url.scheme in ("http", "https") and url.hostname and not url.username and not url.password:
            # Accessing port raises on an out-of-range or invalid port value.
            if url.port is None or 0 < url.port <= 65535:
                return value
    except ValueError:
        pass
    return ""


def text_field(data, key, max_length):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value.strip()) > max_length:
        raise ValueError(f"Недопустимое значение поля {key}.")
    return value.strip()


def phrases(data, key):
    values = data.get(key, [])
    if not isinstance(values, list) or len(values) > 12:
        raise ValueError("Укажите до 12 ключевых фраз в каждом списке.")
    result = {}
    for item in values:
        value = text_field({key: item}, key, 120)
        if value:
            result.setdefault(normalize(value), value)
    return list(result.values())


def library_config(data):
    if not isinstance(data, dict):
        raise ValueError("Ожидался объект с настройками библиотеки.")
    name = text_field(data, "name", 120)
    keywords = phrases(data, "keywords")
    match = data.get("match", "any")
    if not name or match not in ("any", "all"):
        raise ValueError("Укажите название и правило совпадения.")
    # No sources: the library is filled by hand. A source searches by the phrases, so it needs one.
    supplied_sources = data.get("sources", [])
    if not isinstance(supplied_sources, list) or len(supplied_sources) > 20:
        raise ValueError("Добавьте не больше 20 источников.")
    if supplied_sources and not keywords:
        raise ValueError("Для поиска по источникам нужна хотя бы одна ключевая фраза.")
    sources = []
    specs = {spec["kind"]: spec for spec in source_specs()}
    for item in supplied_sources:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or item["kind"] not in specs:
            raise ValueError("Неизвестный тип источника.")
        kind = item["kind"]
        source = {"kind": kind}
        for field in specs[kind]["fields"]:
            field_name = field["key"]
            value = text_field(item, field_name, field["max_length"])
            if not value and field["required"]:
                raise ValueError(f"Заполните поле «{field['label']}».")
            if value:
                if field["type"] == "url" and not web_url(value):
                    raise ValueError(f"Для «{field['label']}» нужна ссылка HTTP(S) без учётных данных.")
                if field.get("pattern") and not re.fullmatch(field["pattern"], value):
                    raise ValueError(f"Проверьте формат поля «{field['label']}».")
                source[field_name] = value
        if source not in sources:
            sources.append(source)
    config = {"name": name, "keywords": keywords, "match": match, "exclude": phrases(data, "exclude"),
              "sources": sources}
    for key, default, minimum, maximum in (("interval_hours", 24, 1, 720), ("lookback_days", 30, 0, 3650)):
        value = data.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"Значение {key} должно быть целым числом от {minimum} до {maximum}.")
        config[key] = value
    for key, default in (("enabled", True), ("download_open_access", True)):
        value = data.get(key, default)
        if type(value) is not bool:
            raise ValueError(f"Ожидалось логическое значение {key}.")
        config[key] = value
    return config


def matcher(config):
    def prepare(text):
        return normalize(re.sub(r"[-\u2010-\u2015\u2212]", " ", text))

    positive = [re.compile(r"(?<!\w)" + re.escape(prepare(term)) + r"(?!\w)")
                for term in config["keywords"]]
    negative = [re.compile(r"(?<!\w)" + re.escape(prepare(term)) + r"(?!\w)")
                for term in config["exclude"]]
    combine = all if config["match"] == "all" else any

    def matches(paper):
        texts = [prepare(str(paper.get(field) or "")) for field in ("title", "abstract")]
        keywords = paper.get("keywords") or []
        texts.extend(prepare(str(keyword)) for keyword in keywords)
        return combine(any(term.search(text) for text in texts) for term in positive) and not any(
            term.search(text) for text in texts for term in negative)

    return matches


def standardized(paper, source):
    if not isinstance(paper, dict):
        raise ValueError("Ожидался объект публикации.")
    if not isinstance(paper.get("title"), str) or not paper["title"].strip():
        raise ValueError("У публикации отсутствует название.")

    kind = source["kind"]
    origin = f"{kind}:{source['url']}" if "url" in source else kind
    fields = {"title": 2000, "abstract": 50000, "published": 10, "venue": 500,
              "external_id": 500, "license": 500, "doi": 700}
    for key in (*fields, "url", "pdf_url"):
        value = paper.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"Поле {key} должно быть текстом.")
        value.encode("utf-8")
    for key in ("authors", "keywords"):
        values = paper.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"Поле {key} должно быть списком строк.")
        for value in values:
            value.encode("utf-8")

    result = {key: paper.get(key, "").strip()[:size] for key, size in fields.items()}
    result["doi"] = normalize_doi(result["doi"])
    result["authors"] = [author[:250] for author in paper.get("authors", [])[:150]]
    result["url"] = web_url(paper.get("url")) or ("https://doi.org/" + result["doi"] if result["doi"] else "")
    result["pdf_url"] = web_url(paper.get("pdf_url"))
    result["source"] = kind
    result["origin"] = origin
    return result


def uploaded_record(file_name, digest):
    """Catalog record for a file added by hand: the file name is the title, the content digest its key,
    so the same file added again, even renamed, finds the same record."""
    title = Path(file_name.replace("\\", "/")).stem.strip()[:2000] or "Без названия"
    return {"title": title, "doi": "", "abstract": "", "authors": [], "venue": "", "published": "",
            "url": "", "pdf_url": "", "license": "", "external_id": digest, "source": "upload", "origin": "upload"}


class Indexer:
    def __init__(self, catalog, *, search=fetch_pages, download=get_bytes, parser=None):
        self.catalog = catalog
        self.search = search
        self.download = download
        self.parser = parser  # MinerU client; None keeps the pypdf text path
        self.wake = threading.Event()
        self.parse_wake = threading.Event()
        self.stop = threading.Event()
        self.discovery = Discovery(self)
        self.page_slots = threading.BoundedSemaphore(2)
        self.rag = Rag(catalog)

    def parse_next(self):
        """Extract one unread PDF locally, or advance one PDF/DjVu job on the configured MinerU server."""
        if self.parser is None:
            item = self.catalog.next_pypdf()
            if not item:
                return False
            article_id, digest = item
            try:
                text, _ = extract(self.catalog.files.path(digest).read_bytes(), "pdf")
            except ValueError as error:
                self.catalog.save_pypdf(article_id, digest, "", str(error))
            except OSError as error:
                self.catalog.save_pypdf(article_id, digest, "", str(error), retry_read=True)
            else:
                self.catalog.save_pypdf(article_id, digest, text)
            return True
        remote = isinstance(self.parser, MinerU)
        item = self.catalog.next_parse(server=self.parser.url, tier=self.parser.tier) if remote else self.catalog.next_parse()
        if item is None:
            return False
        article_id, format_name, digest = item
        source = self.catalog.files.path(digest)
        try:
            with tempfile.TemporaryDirectory() as work:
                def pdf_path():
                    if format_name == "pdf":
                        return source
                    path = Path(work) / "pages.pdf"
                    return path if path.exists() else djvu_to_pdf(source, path)

                name = f"article-{article_id}.pdf"
                if remote:
                    prepared = self.parser.parse_queued(pdf_path, name, self.catalog, article_id, digest,
                                                        prepare=from_mineru)
                    if prepared is None:
                        self.catalog.resume_parse_later(article_id, digest)
                        return True
                else:
                    prepared = from_mineru(*self.parser.parse(pdf_path(), name))
        except ConnectionError as error:
            logging.warning("%s Статья %s вернётся в очередь.", error, article_id)
            self.catalog.resume_parse_later(article_id, digest)
            return False
        except Exception as error:
            logging.warning("Разбор статьи %s не удался: %s", article_id, error)
            self.catalog.parse_failed(article_id, str(error)[:300], expected_digest=digest)
            return True
        self.catalog.save_parsed(article_id, prepared, expected_digest=digest)
        return True

    def parse_loop(self):
        """Worker thread: parse queued PDFs one at a time while the service runs."""
        while not self.stop.is_set():
            self.parse_wake.clear()
            try:
                if self.parse_next():
                    continue
            except Exception:
                logging.exception("Сбой очереди разбора")
            self.parse_wake.wait(30)

    def read_pdf(self, url):
        raw = self.download(url, MAX_DOWNLOAD_BYTES, accept="application/pdf")
        if not has_signature("pdf", raw[:1024]):
            raise ValueError("Сервер вернул файл, который не похож на PDF.")
        return raw

    def store_pdf(self, article_id, raw, provenance=None):
        body, problem = "", ""
        if not self.parser:
            try:
                body, _ = extract(raw, "pdf")
            except ValueError as error:
                problem = str(error)
        with self.catalog.files.stage([raw]) as (digest, staged):
            stored = self.catalog.attach(article_id, "pdf", digest, body, only_missing=True,
                                         error=problem, provenance=provenance, staged=staged)
        if stored and self.parser:
            self.catalog.queue_parse(article_id)
            self.parse_wake.set()
        return stored

    def fetch_pdf(self, article):
        try:
            return self.read_pdf(article["pdf_url"]), None
        except Exception as direct_error:
            errors = [pdf_error("PDF", direct_error) or "Ссылка на PDF больше не доступна."]
        found = resolved_pdf(article["doi"], lambda kind: self.catalog.source_ids(article["id"], kind),
                             self.download, self.read_pdf, errors, skip=[article["pdf_url"]])
        if found:
            return found
        raise ValueError(" · ".join(dict.fromkeys(errors)))

    def supervise(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                if self.run_next():
                    continue
            except Exception:
                logging.exception("Сбой планировщика")
            self.wake.wait(15)

    def run_next(self):
        run = self.catalog.claim()
        if run is None:
            return False
        found, added, downloaded = set(), 0, 0
        errors = []
        successful_queries = 0
        status = "error"
        try:
            cfg = self.catalog.libraries(run["library_id"])[0]
            matches = matcher(cfg)
            days = cfg["lookback_days"]
            since = date.today() - timedelta(days=days) if days else date.min
            specs = {spec["kind"]: spec for spec in source_specs()}
            for source in cfg["sources"]:
                spec = specs.get(source["kind"])
                if not spec:
                    errors.append(f"Источник {source['kind']} недоступен в этой установке.")
                    continue
                label = spec["label"]
                if "url" in source:
                    label += " " + urlsplit(source["url"]).hostname
                terms = [""] if spec["mode"] == "feed" else cfg["keywords"]
                for keyword in terms:
                    try:
                        rows = self.search(source, keyword, since, PAGE_SIZE)
                        pages = iter((rows,)) if isinstance(rows, list) else iter(rows)
                    except Exception as error:
                        errors.append(f"{label} · {keyword or 'RSS'} — {str(error)[:300]}")
                        continue
                    position = 0
                    received_page = False
                    while not self.stop.is_set():
                        try:
                            papers = next(pages)
                            if not isinstance(papers, list):
                                raise ValueError("Адаптер должен вернуть список публикаций.")
                        except StopIteration:
                            successful_queries += 1
                            break
                        except Exception as error:
                            successful_queries += int(received_page)
                            errors.append(f"{label} · {keyword or 'RSS'} — {str(error)[:300]}")
                            break
                        received_page = True
                        for paper in papers:
                            position += 1
                            try:
                                candidate = standardized(paper, source)
                            except ValueError as error:
                                errors.append(f"{label} — публикация {position}: {str(error)[:300]}")
                                continue
                            if matches(paper):
                                article_id, new_membership = self.catalog.add_article(cfg["id"], candidate)
                                if article_id is None:
                                    continue
                                found.add(article_id)
                                added += new_membership
            if cfg["download_open_access"]:
                for doc in self.catalog.pending(cfg["id"], DOWNLOAD_LIMIT):
                    try:
                        raw, provenance = self.fetch_pdf(doc)
                    except Exception as error:
                        self.catalog.download_failed(doc["id"], str(error)[:300])
                        errors.append(f"Статья {doc['id']} — загрузка PDF: {str(error)[:200]}")
                        continue
                    stored = self.store_pdf(doc["id"], raw, provenance=provenance)
                    downloaded += int(stored)
                    if stored and not self.parser and (problem := self.catalog.article(doc["id"])["download_error"]):
                        errors.append(f"Статья {doc['id']} — {problem}")
            # A library without sources has nothing to search, and that is not an error.
            status = ("partial" if errors else "ok") if successful_queries or not cfg["sources"] else "error"
        except Exception as error:
            logging.exception("Сбой обработки библиотеки %s", run["library_id"])
            errors.append(f"Обработка прервана: {str(error)[:300]}")
        finally:
            if len(errors) > 30:
                errors = errors[:30] + [f"Других сообщений: {len(errors) - 30}."]
            self.catalog.finish(run, status, len(found), added, downloaded, errors)
        return True


class HttpError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")

    def do_PUT(self):
        self.serve("PUT")

    def log_request(self, code="-", size="-"):
        if self.path != "/healthz" or code != 200:
            super().log_request(code, size)

    def headers_for(self, status, mime, *, length=None, filename=None, inline=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        ancestors = "'self'" if inline else "'none'"
        objects = "'self'" if inline and mime == "application/pdf" else "'none'"
        self.send_header("Content-Security-Policy", f"default-src 'self'; object-src {objects}; frame-ancestors {ancestors}; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if filename:
            disposition = "inline" if inline else "attachment"
            self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Literature Indexer", charset="UTF-8"')
        self.end_headers()

    def send_bytes(self, status, mime, data):
        self.headers_for(status, mime, length=len(data))
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode())

    def send_file(self, path, mime, filename=None, *, inline=False):
        """Stream a stored file from disk."""
        with path.open("rb") as source:
            self.headers_for(200, mime, length=path.stat().st_size, filename=filename, inline=inline)
            shutil.copyfileobj(source, self.wfile)

    def authorize(self, method):
        host = self.headers.get("Host", "")
        if self.server.password:
            head = self.headers.get("Authorization", "")
            parts = head.split(maxsplit=1)
            supplied = b""
            if len(parts) == 2 and parts[0].lower() == "basic":
                try:
                    supplied = base64.b64decode(parts[1], validate=True)
                except (binascii.Error, ValueError):
                    pass
            expected = "admin:".encode() + self.server.password.encode()
            if not hmac.compare_digest(supplied, expected):
                raise HttpError(401, "Войдите как admin с паролем INDEXER_PASSWORD.")
        elif urlsplit("//" + host).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise HttpError(403, "Для доступа из сети установите INDEXER_PASSWORD.")
        if method != "GET" and self.headers.get("Origin"):
            if urlsplit(self.headers["Origin"]).netloc.lower() != host.lower():
                raise HttpError(403, "Запрос должен исходить с того же адреса, что и интерфейс.")

    def request_body(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(400, "Нужна загрузка с известным размером.")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= limit:
            raise HttpError(413 if length > limit else 400, "Пустое тело запроса или превышен допустимый размер.")
        data = self.rfile.read(length)
        if len(data) != length:
            raise HttpError(400, "Тело запроса получено не полностью.")
        return data

    def request_chunks(self):
        """The request body in 1 MiB pieces, so a file of any size passes without filling memory."""
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(400, "Нужна загрузка с известным размером.")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise HttpError(400, "Файл пуст.")

        def pieces(left):
            while left:
                piece = self.rfile.read(min(left, 1 << 20))
                if not piece:
                    raise HttpError(400, "Тело запроса получено не полностью.")
                left -= len(piece)
                yield piece

        return pieces(length)

    def receive_file(self, format_name):
        """Check the uploaded file's signature on its first piece and stream it into the file store."""
        if format_name == "djvu" and not self.server.indexer.parser:
            raise HttpError(422, "DjVu разбирается только через MinerU: задайте MINERU_URL.")
        body = self.request_chunks()
        first = next(body)
        if not has_signature(format_name, first):
            raise HttpError(422, f"Файл не распознан как {format_name.upper()}.")
        return self.server.indexer.catalog.files.stage(chain([first], body))

    def extract_text(self, format_name, staged):
        """Text of a received file; empty for PDF and DjVu that MinerU will parse."""
        if format_name in PARSED_FORMATS and self.server.indexer.parser:
            return ""
        try:
            return extract(staged.read_bytes(), format_name)[0]
        except ValueError as error:
            raise HttpError(422, str(error)) from error

    def store_file(self, article, format_name, digest, text, staged):
        """Attach a received file to the article and queue it for MinerU when it has no text yet."""
        indexer = self.server.indexer
        catalog = indexer.catalog
        if not article or not catalog.attach(article["id"], format_name, digest, text, staged=staged):
            raise HttpError(404, "Запись больше не существует. Обновите библиотеку и повторите загрузку.")
        local = format_name in PARSED_FORMATS or any(catalog.attachment(article["id"], kind) for kind in PARSED_FORMATS)
        if text:
            return {"content_kind": "full_text", "local_pdf": local}
        catalog.queue_parse(article["id"])
        indexer.parse_wake.set()
        return {"content_kind": content_kind("", article["abstract"]), "local_pdf": True, "parse_status": "pending"}

    def add_library_file(self, library_id, query):
        """Add one file to a library as its own record; the same file again is reported as a duplicate."""
        catalog = self.server.indexer.catalog
        name = query.get("name", [""])[0][:300]
        format_name = UPLOAD_FORMATS.get(Path(name).suffix.lower().lstrip("."))
        if not format_name:
            raise HttpError(415, "Поддерживаются PDF, DjVu, XML, Markdown и TXT.")
        with self.receive_file(format_name) as (digest, staged):
            text = self.extract_text(format_name, staged)
            article_id, added = catalog.add_article(library_id, uploaded_record(name, digest))
            if article_id is None:
                raise HttpError(409, "Статья исключена. Восстановите её в списке исключённых.")
            result = {"id": article_id, "duplicate": not added}
            if catalog.attachment(article_id, format_name) == digest:
                return result
            return result | self.store_file(catalog.article(article_id), format_name, digest, text, staged)

    def request_json(self):
        if self.headers.get_content_type() != "application/json":
            raise HttpError(415, "Ожидалось application/json.")
        return json.loads(self.request_body(64 * 1024))

    def serve(self, method):
        try:
            route = urlsplit(self.path)
            if method == "GET" and route.path == "/healthz":
                self.health()
                return
            self.authorize(method)
            self.dispatch(method, route.path, parse_qs(route.query))
        except HttpError as error:
            self.send_json({"error": str(error)}, error.status)
        except RagUnavailable as error:
            self.send_json({"error": str(error)}, 503)
        except KeyError as error:
            self.send_json({"error": f"Запись не найдена: {error}."}, 404)
        except (ValueError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.exception("Ошибка HTTP")
            self.send_json({"error": "Внутренняя ошибка. Подробности в журнале сервиса."}, 500)

    def health(self):
        if self.server.worker is not None and not self.server.worker.is_alive():
            self.send_json({"status": "unavailable"}, 503)
            return
        if not self.server.indexer.catalog.available():
            self.send_json({"status": "unavailable"}, 503)
            return
        self.send_json({"status": "ok"})

    def dispatch(self, method, path, query):
        indexer = self.server.indexer
        catalog = indexer.catalog
        if path == "/mcp" and method == "POST":
            result = indexer.rag.mcp(self.request_json())
            if result is None:
                self.headers_for(202, "application/json", length=0)
            else:
                self.send_json(result)
            return
        if path == "/mcp":
            raise HttpError(405, "Для MCP используйте POST.")
        if path == "/api/rag/settings" and method in ("GET", "PUT"):
            settings = indexer.rag.settings() if method == "GET" else indexer.rag.save_settings(self.request_json())
            self.send_json(settings)
            return
        profiles = re.fullmatch(r"/api/rag/profiles/(\d{1,2})/models", path)
        if method == "GET" and profiles:
            self.send_json({"models": indexer.rag.models(int(profiles[1]))})
            return
        page = re.fullmatch(r"/api/articles/([1-9]\d{0,9})/pages/([1-9]\d{0,5})\.png", path)
        if method == "GET" and page:
            aid = int(page[1])
            digest = catalog.attachment(aid, "pdf")
            if not digest or not catalog.visible(aid):
                raise HttpError(404, "PDF не найден.")
            if not indexer.page_slots.acquire(timeout=3):
                raise HttpError(503, "Другие страницы ещё открываются. Повторите запрос.")
            try:
                data = pdf_page_png(catalog.files.path(digest), int(page[2]))
            except FileNotFoundError as exc:
                raise HttpError(503, "Просмотр страницы временно недоступен.") from exc
            except ValueError as exc:
                raise HttpError(422, str(exc)) from exc
            finally:
                indexer.page_slots.release()
            if not catalog.visible(aid) or catalog.attachment(aid, "pdf") != digest:
                raise HttpError(404, "Исходный PDF изменился.")
            self.send_bytes(200, "image/png", data)
            return
        passages = re.fullmatch(r"/api/libraries/([1-9]\d*)/articles/([1-9]\d*)/passages", path)
        if method == "GET" and passages:
            try:
                result = indexer.rag.read(int(passages[1]), int(passages[2]),
                                          int(query["page"][0]) if "page" in query else None,
                                          int(query.get("offset", [0])[0]))
            except LookupError as exc:
                raise HttpError(404, str(exc)) from exc
            self.send_json(result)
            return
        library_action = re.fullmatch(r"/api/libraries/([1-9]\d*)/(resolve|rag/(?:ask|status|search)|articles/([1-9]\d*)/(exclude|restore|resolve|reparse))", path)
        if library_action:
            lid, action = int(library_action[1]), library_action[2]
            if not catalog.libraries(lid):
                raise HttpError(404, "Библиотека не найдена.")
            if action == "rag/status" and method == "GET":
                self.send_json(indexer.rag.status(lid) | {"discovery": indexer.discovery.status(lid)})
                return
            if method != "POST":
                raise HttpError(405, "Действие требует POST.")
            aid = int(library_action[3]) if library_action[3] else None
            if action == "resolve" or library_action[4] == "resolve":
                self.send_json(indexer.discovery.queue(lid, aid), 202)
                return
            if library_action[4] == "reparse":
                if not indexer.parser or not catalog.visible(aid, lid) or not any(
                    catalog.attachment(aid, fmt) for fmt in PARSED_FORMATS
                ):
                    raise HttpError(409, "Для повторного разбора нужны исходный файл и подключённый MinerU.")
                catalog.queue_parse(aid)
                indexer.parse_wake.set()
                self.send_json({"queued": 1}, 202)
                return
            data = self.request_json()
            if not isinstance(data, dict):
                raise ValueError("Нужен JSON-объект.")
            if action in ("rag/ask","rag/search"):
                question = text_field(data, "query", 4000)
                limit = data.get("limit", 5)
                if not question or type(limit) is not int or not 1 <= limit <= 12:
                    raise ValueError("Введите вопрос и количество фрагментов от 1 до 12.")
                result = (indexer.rag.ask(lid, question, limit, model=data.get("model")) if action == "rag/ask"
                          else {"sources": indexer.rag.search(lid, question, limit)})
                self.send_json(result)
                return
            if library_action[4] in ("exclude","restore"):
                global_scope, purge = data.get("global_scope", False), data.get("purge", False)
                if type(global_scope) is not bool or type(purge) is not bool:
                    raise ValueError("Ожидается логическое значение.")
                result = (catalog.exclude(lid, aid, global_scope=global_scope, purge=purge)
                          if library_action[4] == "exclude" else catalog.restore(lid, aid, global_scope=global_scope))
                self.send_json(result)
                return
            raise HttpError(405, "Метод не поддерживается.")
        static_types = {"/": ("index.html", "text/html"), "/static/app.js": ("app.js", "text/javascript"),
                        "/static/style.css": ("style.css", "text/css")}
        if method == "GET" and path in static_types:
            filename, mime = static_types[path]
            self.send_bytes(200, mime + "; charset=utf-8", (STATIC / filename).read_bytes())
            return
        if path == "/api/libraries":
            if method == "GET":
                self.send_json(catalog.libraries())
                return
            if method == "POST":
                result = catalog.save_library(library_config(self.request_json()))
                self.server.indexer.wake.set()
                self.send_json(result, 201)
                return
        if method == "GET" and path == "/api/sources":
            self.send_json(source_specs())
            return
        match = re.fullmatch(r"/api/files/([0-9a-f]{64})\.(jpg|jpeg|png)", path)
        if method == "GET" and match:
            picture = catalog.files.path(match[1])
            if not picture.is_file() or not catalog.visible_figure(match[1], match[2]):
                raise HttpError(404, "Файл не найден.")
            self.send_file(picture, PICTURE_TYPES[match[2]])
            return
        match = re.fullmatch(r"/api/libraries/([1-9]\d{0,9})(?:/(run|runs|articles|files|export\.md|export\.jsonl))?",
                             path)
        if match:
            library_id = int(match[1])
            rows = catalog.libraries(library_id)
            if not rows:
                raise HttpError(404, "Библиотека не найдена.")
            if method == "PUT" and match[2] is None:
                result = catalog.save_library(library_config(self.request_json()), library_id)
                self.server.indexer.wake.set()
                self.send_json(result)
                return
            if method == "POST" and match[2] == "run":
                status = catalog.queue(library_id)
                self.server.indexer.wake.set()
                self.send_json({"status": status}, 202)
                return
            if method == "GET" and match[2] == "runs":
                self.send_json(catalog.runs(library_id))
                return
            if method == "POST" and match[2] == "files":
                self.send_json(self.add_library_file(library_id, query), 201)
                return
            if method == "GET" and match[2] == "articles":
                offset = max(0, min(10**8, int(query.get("offset", [0])[0])))
                scope = query.get("scope", ["all"])[0]
                if scope not in ("all", "full_text", "pdf", "metadata_only", "no_pdf", "parse_pending",
                                 "parse_failed", "download_failed", "excluded"):
                    raise HttpError(400, "Неизвестный фильтр статей.")
                items, total = catalog.articles(library_id, query.get("search", [""])[0][:300], 50, offset, scope=scope)
                indexer.discovery.annotate(items)
                self.send_json({"items": items, "total": total, "limit": 50, "offset": offset})
                return
            if method == "GET" and match[2] in ("export.md", "export.jsonl"):
                self.export(rows[0], match[2].split(".")[1], query.get("full_text") == ["1"])
                return
        match = re.fullmatch(r"/api/articles/([1-9]\d{0,9})/(text|pdf|file)", path)
        if match:
            self.article_request(method, int(match[1]), match[2], query)
            return
        raise HttpError(404, "Страница не найдена.")

    def article_request(self, method, article_id, action, query):
        catalog = self.server.indexer.catalog
        article = catalog.article(article_id)
        if not article or not catalog.visible(article_id):
            raise HttpError(404, "Статья не найдена.")
        if method == "GET" and action == "text":
            self.send_json({"text": article["body"] or article["abstract"],
                            "content_kind": content_kind(article["body"], article["abstract"])})
            return
        if method == "GET" and action == "pdf":
            for format_name, mime in (("pdf", "application/pdf"), ("djvu", "image/vnd.djvu")):
                digest = catalog.attachment(article_id, format_name)
                if digest:
                    self.send_file(catalog.files.path(digest), mime, f"article-{article_id}.{format_name}",
                                   inline=format_name == "pdf" and query.get("inline") == ["1"])
                    return
            raise HttpError(404, "В библиотеке нет файла этой статьи.")
        if method == "PUT" and action == "file":
            format_name = query.get("format", [""])[0]
            if format_name not in set(UPLOAD_FORMATS.values()):
                raise HttpError(415, "Поддерживаются PDF, DjVu, XML, Markdown и TXT.")
            with self.receive_file(format_name) as (digest, staged):
                self.send_json(self.store_file(article, format_name, digest, self.extract_text(format_name, staged),
                                               staged))
            return
        raise HttpError(404, "Действие не найдено.")

    def export(self, library, format_name, full_text_only):
        catalog = self.server.indexer.catalog
        rows = catalog.export_articles(library["id"], full_text_only)
        markdown = format_name == "md"
        mime = "text/markdown" if markdown else "application/x-ndjson"
        self.headers_for(200, mime + "; charset=utf-8", filename=f"library-{library['id']}.{format_name}")
        if markdown:
            self.wfile.write(f"# {library['name']}\n\n".encode())
        for article in rows:
            abstract = article["abstract"] if allows_abstract_export(article["abstract_source"]) else ""
            kind = content_kind(article["body"], abstract)
            meta = {key: article[key] for key in ("title", "doi", "url", "venue", "published", "license")}
            authors = json.loads(article["authors"] or "[]")
            body = article["body"] or abstract
            if markdown:
                label = {"full_text": "Полный текст", "abstract": "Аннотация", "metadata": "Только метаданные"}[kind]
                lines = ["---", "", f"# {meta['title']}", "", label,
                         ", ".join(filter(None, (", ".join(authors), meta["venue"], meta["published"]))),
                         " · ".join(filter(None, (meta["doi"], meta["url"]))), "", body, "", ""]
                self.wfile.write("\n".join(lines).encode())
                continue
            for index, chunk in enumerate(chunks(body or meta["title"])):
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"scisearcher:{catalog.identity}:{article['id']}:{index}"))
                row = meta | chunk | {"id": point_id, "article_id": article["id"],
                                      "library_id": library["id"], "library": library["name"], "authors": authors,
                                      "library_ids": sorted(map(int, article["library_ids"].split(","))),
                                      "sources": (article["sources"] or "").split(","), "content_kind": kind}
                self.wfile.write((json.dumps(row, ensure_ascii=False) + "\n").encode())


def make_server(host, port, indexer, password="", *, worker=None):
    server = ThreadingHTTPServer((host, port), Api)
    server.indexer = indexer
    server.password = password
    server.worker = worker
    return server


def main():
    # Windows uses the ANSI code page for redirected output; Russian messages need UTF-8.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = argparse.ArgumentParser(description="Локальный индексер научной литературы")
    args.add_argument("--host", default=os.environ.get("INDEXER_HOST", "127.0.0.1"))
    args.add_argument("--port", type=int, default=int(os.environ.get("INDEXER_PORT", "8080")))
    args.add_argument("--data", type=Path, default=Path(os.environ.get("INDEXER_DATA_DIR", "data")))
    config = args.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    password = os.environ.get("INDEXER_PASSWORD", "")
    if not password and config.host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning("INDEXER_PASSWORD не задан, а сервис слушает %s. Проверка заголовка Host не защищает "
                        "от клиентов в сети: они могут прислать Host: localhost.", config.host)
    mineru_url = os.environ.get("MINERU_URL", "")
    timeout = float(os.environ.get("MINERU_TIMEOUT_HOURS", "48")) * 3600
    parser = MinerU(mineru_url, os.environ.get("MINERU_API_KEY", ""), os.environ.get("MINERU_TIER", ""),
                    timeout_seconds=timeout) if mineru_url else None
    indexer = Indexer(Catalog(config.data / "catalog.sqlite3"), parser=parser)
    worker = threading.Thread(target=indexer.supervise, daemon=True)
    server = make_server(config.host, config.port, indexer, password, worker=worker)
    worker.start()
    threading.Thread(target=indexer.discovery.run, daemon=True).start()
    threading.Thread(target=indexer.rag.run, args=(indexer.stop,), daemon=True).start()
    queued = indexer.catalog.queue_unparsed_pdfs(mineru=parser is not None)
    workers = max(1, int(os.environ.get("PARSE_WORKERS", "1"))) if parser else 1
    for _ in range(workers):
        threading.Thread(target=indexer.parse_loop, daemon=True).start()
    if parser:
        logging.info("PDF разбирает MinerU: %s, параллельно %s, поставлено в очередь %s", mineru_url, workers, queued)
    else:
        logging.info("pypdf извлекает текст из сохранённых PDF в фоновом режиме.")
    # The bind address, not server_name: a reverse-DNS name (e.g. kubernetes.docker.internal) fails the Host check.
    print(f"Индексер слушает http://{config.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        indexer.stop.set()
        indexer.wake.set()
        indexer.parse_wake.set()
        indexer.discovery.wake.set()
        server.server_close()


if __name__ == "__main__":
    main()
