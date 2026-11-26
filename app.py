import argparse
import base64
import binascii
import hmac
import json
import logging
import os
import re
import threading
import uuid
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from catalog import Catalog, content_kind, normalize, normalize_doi
from documents import MAX_UPLOAD_BYTES, chunks, extract
from sources import fetch, get_bytes, source_specs


STATIC = Path(__file__).resolve().parent / "static"
RESULT_LIMIT = 100
DOWNLOAD_LIMIT = 40


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
    if not name or not keywords or match not in ("any", "all"):
        raise ValueError("Укажите название, хотя бы одну ключевую фразу и правило совпадения.")
    supplied_sources = data.get("sources")
    if not isinstance(supplied_sources, list) or not 1 <= len(supplied_sources) <= 20:
        raise ValueError("Добавьте от 1 до 20 источников.")
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
    for key, default, maximum in (("interval_hours", 24, 720), ("lookback_days", 30, 3650)):
        value = data.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"Значение {key} должно быть целым числом от 1 до {maximum}.")
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
    kind = source["kind"]
    origin = f"{kind}:{source['url']}" if "url" in source else kind
    fields = {"title": 2000, "abstract": 50000, "published": 10, "venue": 500,
              "external_id": 500, "license": 500, "doi": 700}
    result = {key: str(paper.get(key) or "").strip()[:size] for key, size in fields.items()}
    result["doi"] = normalize_doi(result["doi"])
    result["authors"] = [str(author)[:250] for author in (paper.get("authors") or [])[:150]]
    result["url"] = web_url(paper.get("url")) or ("https://doi.org/" + result["doi"] if result["doi"] else "")
    result["pdf_url"] = web_url(paper.get("pdf_url"))
    result["source"] = kind
    result["origin"] = origin
    return result


class Indexer:
    def __init__(self, catalog, *, search=fetch, download=get_bytes):
        self.catalog = catalog
        self.search = search
        self.download = download
        self.wake = threading.Event()
        self.stop = threading.Event()

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
            since = date.today() - timedelta(days=cfg["lookback_days"])
            specs = {spec["kind"]: spec for spec in source_specs()}
            for source in cfg["sources"]:
                spec = specs[source["kind"]]
                label = spec["label"]
                if "url" in source:
                    label += " " + urlsplit(source["url"]).hostname
                terms = [""] if spec["mode"] == "feed" else cfg["keywords"]
                for keyword in terms:
                    try:
                        papers = self.search(source, keyword, since, RESULT_LIMIT)
                        successful_queries += 1
                        if len(papers) >= RESULT_LIMIT:
                            errors.append(f"{label} — достигнут предел выдачи ({RESULT_LIMIT}) по запросу {keyword or 'RSS'}.")
                    except Exception as error:
                        errors.append(f"{label} — {str(error)[:300]}")
                        continue
                    for paper in papers:
                        if paper.get("title") and matches(paper):
                            article_id, new_membership = self.catalog.add_article(cfg["id"], standardized(paper, source))
                            found.add(article_id)
                            added += new_membership
            if cfg["download_open_access"]:
                for doc in self.catalog.pending(cfg["id"], DOWNLOAD_LIMIT):
                    try:
                        raw = self.download(doc["pdf_url"], MAX_UPLOAD_BYTES, accept="application/pdf")
                        if b"%PDF-" not in raw[:1024]:
                            raise ValueError("Сервер вернул файл, который не похож на PDF.")
                    except Exception as error:
                        self.catalog.download_failed(doc["id"], str(error)[:300])
                        errors.append(f"Статья {doc['id']} — загрузка PDF: {str(error)[:200]}")
                        continue
                    problem = ""
                    try:
                        text, _ = extract(raw, "pdf")
                    except ValueError as error:
                        text = ""
                        problem = str(error)
                        errors.append(f"Статья {doc['id']} — {problem}")
                    downloaded += int(self.catalog.attach(doc["id"], "pdf", raw, text, only_missing=True, error=problem))
            status = ("partial" if errors else "ok") if successful_queries else "error"
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

    def headers_for(self, status, mime, *, length=None, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Literature Indexer", charset="UTF-8"')
        self.end_headers()

    def send_bytes(self, status, mime, data):
        self.headers_for(status, mime, length=len(data))
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode())

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

    def request_json(self):
        if self.headers.get_content_type() != "application/json":
            raise HttpError(415, "Ожидалось application/json.")
        return json.loads(self.request_body(64 * 1024))

    def serve(self, method):
        try:
            self.authorize(method)
            route = urlsplit(self.path)
            self.dispatch(method, route.path, parse_qs(route.query))
        except HttpError as error:
            self.send_json({"error": str(error)}, error.status)
        except (ValueError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.exception("Ошибка HTTP")
            self.send_json({"error": "Внутренняя ошибка. Подробности в журнале сервиса."}, 500)

    def dispatch(self, method, path, query):
        catalog = self.server.indexer.catalog
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
        match = re.fullmatch(r"/api/libraries/([1-9]\d{0,9})(?:/(run|runs|articles|export\.md|export\.jsonl))?", path)
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
            if method == "GET" and match[2] == "articles":
                offset = max(0, min(10**8, int(query.get("offset", [0])[0])))
                items, total = catalog.articles(library_id, query.get("search", [""])[0][:300], 50, offset)
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
        if not article:
            raise HttpError(404, "Статья не найдена.")
        if method == "GET" and action == "text":
            self.send_json({"text": article["body"] or article["abstract"],
                            "content_kind": content_kind(article["body"], article["abstract"])})
            return
        if method == "GET" and action == "pdf":
            file = catalog.attachment(article_id, "pdf")
            if file is None:
                raise HttpError(404, "В библиотеке нет PDF этой статьи.")
            self.headers_for(200, "application/pdf", length=len(file), filename=f"article-{article_id}.pdf")
            self.wfile.write(file)
            return
        if method == "PUT" and action == "file":
            format_name = query.get("format", [""])[0]
            if format_name not in ("pdf", "xml", "md", "txt"):
                raise HttpError(415, "Поддерживаются PDF, XML, Markdown и TXT.")
            raw = self.request_body(MAX_UPLOAD_BYTES)
            try:
                text, _ = extract(raw, format_name)
            except ValueError as error:
                raise HttpError(422, str(error)) from error
            catalog.attach(article_id, format_name, raw, text)
            self.send_json({"content_kind": "full_text", "local_pdf": format_name == "pdf" or
                            catalog.attachment(article_id, "pdf") is not None})
            return
        raise HttpError(404, "Действие не найдено.")

    def export(self, library, format_name, full_text_only):
        rows = self.server.indexer.catalog.export_articles(library["id"], full_text_only)
        markdown = format_name == "md"
        mime = "text/markdown" if markdown else "application/x-ndjson"
        self.headers_for(200, mime + "; charset=utf-8", filename=f"library-{library['id']}.{format_name}")
        if markdown:
            self.wfile.write(f"# {library['name']}\n\n".encode())
        for article in rows:
            abstract = "" if article["abstract_source"] == "ieee" else article["abstract"]
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
                identity = article["doi"] or article["url"] or f"{article['title']}:{article['id']}"
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"scisearcher:{identity}:{index}"))
                row = meta | chunk | {"id": point_id, "article_id": article["id"],
                                      "library_id": library["id"], "library": library["name"], "authors": authors,
                                      "library_ids": sorted(map(int, article["library_ids"].split(","))),
                                      "sources": (article["sources"] or "").split(","), "content_kind": kind}
                self.wfile.write((json.dumps(row, ensure_ascii=False) + "\n").encode())


def make_server(host, port, indexer, password=""):
    server = ThreadingHTTPServer((host, port), Api)
    server.indexer = indexer
    server.password = password
    return server


def main():
    args = argparse.ArgumentParser(description="Локальный индексер научной литературы")
    args.add_argument("--host", default=os.environ.get("INDEXER_HOST", "127.0.0.1"))
    args.add_argument("--port", type=int, default=int(os.environ.get("INDEXER_PORT", "8080")))
    args.add_argument("--data", type=Path, default=Path(os.environ.get("INDEXER_DATA_DIR", "data")))
    config = args.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    indexer = Indexer(Catalog(config.data / "catalog.sqlite3"))
    server = make_server(config.host, config.port, indexer, os.environ.get("INDEXER_PASSWORD", ""))
    worker = threading.Thread(target=indexer.supervise, daemon=True)
    worker.start()
    print(f"Индексер слушает http://{server.server_name}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        indexer.stop.set()
        indexer.wake.set()
        server.server_close()


if __name__ == "__main__":
    main()
