import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit, urlunsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_doi(value):
    value = unquote(value.strip()).lower()
    value = re.sub(r"^(https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s]+", value) else ""


def aliases(paper):
    keys = []
    if paper["doi"]:
        keys.append("doi:" + paper["doi"])
    url = urlsplit(paper["url"])
    if url.scheme in ("http", "https") and url.netloc:
        # Preserve the query string: some journals identify articles by query parameter.
        permalink = urlunsplit(("https", url.netloc.lower(), url.path, url.query, ""))
        keys.append("url:" + permalink)
    if paper["external_id"]:
        keys.append(f"{paper['origin']}:{paper['external_id']}")
    if not keys:
        text = "\n".join((paper["origin"], paper["title"], paper["published"], *paper["authors"]))
        keys.append("metadata:" + hashlib.sha256(normalize(text).encode()).hexdigest())
    return keys


ARTICLE_FIELDS = ("title", "doi", "authors", "venue", "published", "url", "pdf_url", "license")
DOWNLOAD_FIELDS = ("body", "body_format", "download_status", "download_error", "retry_at", "download_attempts")


def abstract_priority(text, source):
    return bool(text), source != "ieee", len(text)


SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY,
    config TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    due_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    doi TEXT NOT NULL,
    abstract TEXT NOT NULL,
    abstract_source TEXT NOT NULL,
    authors TEXT NOT NULL,
    venue TEXT NOT NULL,
    published TEXT NOT NULL,
    url TEXT NOT NULL,
    pdf_url TEXT NOT NULL,
    license TEXT NOT NULL,
    search_text TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    body_format TEXT NOT NULL DEFAULT '',
    download_status TEXT NOT NULL DEFAULT 'unavailable',
    download_error TEXT NOT NULL DEFAULT '',
    retry_at TEXT NOT NULL DEFAULT '',
    download_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS article_keys (
    key TEXT PRIMARY KEY,
    article_id INTEGER NOT NULL REFERENCES articles(id)
);
CREATE INDEX IF NOT EXISTS article_keys_article_id ON article_keys (article_id);
CREATE TABLE IF NOT EXISTS article_sources (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    source TEXT NOT NULL,
    origin TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (article_id, origin)
);
CREATE TABLE IF NOT EXISTS memberships (
    library_id INTEGER NOT NULL REFERENCES libraries(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    PRIMARY KEY (library_id, article_id)
);
CREATE TABLE IF NOT EXISTS attachments (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    format TEXT NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (article_id, format)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    library_id INTEGER NOT NULL REFERENCES libraries(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS runs_library ON runs (library_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS runs_active_library ON runs (library_id)
    WHERE status IN ('queued', 'running');
"""


class Catalog:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO catalog_metadata VALUES ('identity', ?)", (str(uuid.uuid4()),))
            self.identity = db.execute("SELECT value FROM catalog_metadata WHERE key = 'identity'").fetchone()[0]
            # Merges can delete the highest article ID; never allocate it again.
            db.execute("""INSERT OR IGNORE INTO catalog_metadata
                SELECT 'last_article_id', COALESCE(MAX(id), 0) FROM articles""")
            db.execute("UPDATE runs SET status = 'queued' WHERE status = 'running'")

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def available(self):
        path = self.path.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(path, uri=True, timeout=2)) as db:
                db.execute("SELECT id FROM libraries LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def libraries(self, library_id=None):
        query = """SELECT l.*,
            (SELECT COUNT(*) FROM memberships m WHERE m.library_id = l.id) AS article_count,
            (SELECT COUNT(*) FROM memberships m JOIN articles a ON a.id = m.article_id
                WHERE m.library_id = l.id AND a.body <> '') AS full_text_count,
            (SELECT COUNT(*) FROM memberships m JOIN attachments f ON f.article_id = m.article_id
                WHERE m.library_id = l.id AND f.format = 'pdf') AS pdf_count,
            (SELECT started_at FROM runs r WHERE r.library_id = l.id ORDER BY id DESC LIMIT 1) AS last_run_at,
            (SELECT status FROM runs r WHERE r.library_id = l.id ORDER BY id DESC LIMIT 1) AS last_status
            FROM libraries l"""
        params = ()
        if library_id is not None:
            query += " WHERE l.id = ?"
            params = (library_id,)
        with self.connection() as db:
            return [json.loads(row["config"]) | {
                key: row[key] for key in ("id", "article_count", "full_text_count", "pdf_count", "last_run_at", "last_status")
            } for row in db.execute(query + " ORDER BY l.id", params)]

    def save_library(self, config, library_id=None):
        with self.connection(write=True) as db:
            values = (json.dumps(config, ensure_ascii=False), config["enabled"], now())
            if library_id is None:
                library_id = db.execute(
                    "INSERT INTO libraries (config, enabled, due_at) VALUES (?, ?, ?)", values
                ).lastrowid
            else:
                db.execute("UPDATE libraries SET config = ?, enabled = ?, due_at = ? WHERE id = ?",
                           (*values, library_id))
        return self.libraries(library_id)[0]

    def queue(self, library_id):
        with self.connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO runs (library_id, started_at, status) VALUES (?, ?, 'queued')",
                       (library_id, now()))
            return db.execute(
                "SELECT status FROM runs WHERE library_id = ? ORDER BY id DESC LIMIT 1", (library_id,)
            ).fetchone()[0]

    def claim(self):
        stamp = now()
        with self.connection(write=True) as db:
            db.execute("""INSERT OR IGNORE INTO runs (library_id, started_at, status)
                SELECT id, ?, 'queued' FROM libraries WHERE enabled AND due_at <= ?""", (stamp, stamp))
            row = db.execute("SELECT id, library_id FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?", (stamp, row["id"]))
        return row

    def finish(self, run, status, found, added, downloaded, errors):
        finished = datetime.now(timezone.utc)
        with self.connection(write=True) as db:
            db.execute("""UPDATE runs SET status = ?, finished_at = ?, found = ?, added = ?, downloaded = ?,
                errors = ? WHERE id = ?""", (status, finished.isoformat(), found, added,
                                               downloaded, json.dumps(errors, ensure_ascii=False), run["id"]))
            config, due_at = db.execute("SELECT config, due_at FROM libraries WHERE id = ?",
                                        (run["library_id"],)).fetchone()
            interval = json.loads(config)["interval_hours"]
            started = db.execute("SELECT started_at FROM runs WHERE id = ?", (run["id"],)).fetchone()[0]
            if due_at <= started:
                due_at = (finished + timedelta(hours=interval)).isoformat()
                db.execute("UPDATE libraries SET due_at = ? WHERE id = ?", (due_at, run["library_id"]))

    def runs(self, library_id):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM runs WHERE library_id = ? ORDER BY id DESC LIMIT 30", (library_id,))
            return [dict(row) | {"errors": json.loads(row["errors"])} for row in rows]

    def _matching_articles(self, db, keys, doi):
        hits = {}
        for key in keys:
            hit = db.execute("SELECT a.* FROM article_keys k JOIN articles a ON a.id = k.article_id WHERE k.key = ?",
                             (key,)).fetchone()
            if hit:
                hits[hit["id"]] = hit
        doi = doi or next((hit["doi"] for hit in hits.values() if hit["doi"]), "")
        matches = []
        for hit in hits.values():
            if hit["doi"] and hit["doi"] != doi:
                continue
            if matches:
                ids = [match["id"] for match in matches]
                placeholders = ", ".join("?" for _ in ids)
                conflict = db.execute(f"""SELECT b.article_id FROM attachments a JOIN attachments b
                    ON b.format = a.format WHERE a.article_id = ? AND b.article_id IN ({placeholders})
                    AND a.data <> b.data LIMIT 1""", (hit["id"], *ids)).fetchone()
                if conflict:
                    # One file per format is addressable. Keep both originals on conflict.
                    logging.warning("Different attachments prevent merging articles %s and %s", hit["id"], conflict[0])
                    continue
            matches.append(hit)
        return sorted(matches, key=lambda row: row["id"])

    def _merge_articles(self, db, matches):
        winner = dict(matches[0])
        if len(matches) == 1:
            return winner

        winner_id = winner["id"]
        best = max(matches, key=lambda row: abstract_priority(row["abstract"], row["abstract_source"]))
        winner.update(abstract=best["abstract"], abstract_source=best["abstract_source"])
        for article in matches[1:]:
            duplicate_id = article["id"]
            for field in ARTICLE_FIELDS:
                winner[field] = winner[field] or article[field]
            db.execute("UPDATE article_keys SET article_id = ? WHERE article_id = ?", (winner_id, duplicate_id))
            db.execute("""INSERT OR IGNORE INTO article_sources
                SELECT ?, source, origin, url FROM article_sources WHERE article_id = ?""", (winner_id, duplicate_id))
            db.execute("""INSERT OR IGNORE INTO memberships
                SELECT library_id, ? FROM memberships WHERE article_id = ?""", (winner_id, duplicate_id))
            db.execute("""INSERT OR IGNORE INTO attachments
                SELECT ?, format, data FROM attachments WHERE article_id = ?""", (winner_id, duplicate_id))
            db.execute("DELETE FROM article_sources WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM memberships WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM attachments WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM articles WHERE id = ?", (duplicate_id,))

        state = max(matches, key=lambda row: (bool(row["body"]), row["download_status"] == "pending",
                                               row["download_status"] == "failed", row["download_status"] == "no_text"))
        winner.update({field: state[field] for field in DOWNLOAD_FIELDS})
        if pdf := (state if state["pdf_url"] else next((row for row in matches if row["pdf_url"]), None)):
            winner.update(pdf_url=pdf["pdf_url"], license=pdf["license"])
        return winner

    def add_article(self, library_id, paper):
        keys = aliases(paper)
        with self.connection(write=True) as db:
            matches = self._matching_articles(db, keys, paper["doi"])
            article = self._merge_articles(db, matches) if matches else None
            data = {field: paper[field] for field in ARTICLE_FIELDS}
            data["abstract"] = paper["abstract"]
            data["authors"] = json.dumps(data["authors"], ensure_ascii=False) if data["authors"] else ""
            data["abstract_source"] = paper["source"] if paper["abstract"] else ""
            if article:
                for field in ARTICLE_FIELDS:
                    data[field] = article[field] or data[field]
                if abstract_priority(article["abstract"], article["abstract_source"]) >= abstract_priority(
                        data["abstract"], data["abstract_source"]):
                    data.update(abstract=article["abstract"], abstract_source=article["abstract_source"])
                if len(matches) > 1:
                    data.update({field: article[field] for field in DOWNLOAD_FIELDS})
                if data["pdf_url"]:
                    data["license"] = article["license"] if article["pdf_url"] else paper["license"]
                if (not article["body"] and paper["pdf_url"] and paper["pdf_url"] != article["pdf_url"]
                        and article["download_status"] in ("failed", "no_text")):
                    # A later source may supply a working copy of the same publication.
                    data.update(pdf_url=paper["pdf_url"], license=paper["license"],
                                retry_at=now(), download_status="pending", download_error="")
            data["search_text"] = normalize(" ".join(data[field] for field in ("title", "doi", "abstract", "authors", "venue")))
            if article:
                columns = ", ".join(f"{field} = ?" for field in data)
                article_id = article["id"]
                db.execute(f"UPDATE articles SET {columns} WHERE id = ?", (*data.values(), article_id))
            else:
                article_id = int(db.execute("""UPDATE catalog_metadata SET value = CAST(value AS INTEGER) + 1
                    WHERE key = 'last_article_id' RETURNING value""").fetchone()[0])
                data["id"] = article_id
                columns = ", ".join(data)
                slots = ", ".join("?" for _ in data)
                db.execute(f"INSERT INTO articles ({columns}) VALUES ({slots})", tuple(data.values()))
            db.executemany("INSERT OR IGNORE INTO article_keys VALUES (?, ?)", [(key, article_id) for key in keys])
            db.execute("INSERT OR IGNORE INTO article_sources VALUES (?, ?, ?, ?)",
                       (article_id, paper["source"], paper["origin"], paper["url"]))
            added = db.execute("INSERT OR IGNORE INTO memberships VALUES (?, ?)", (library_id, article_id)).rowcount
            db.execute("UPDATE articles SET download_status = 'pending', retry_at = ? WHERE id = ? AND pdf_url <> '' "
                       "AND download_status = 'unavailable'", (now(), article_id))
            return article_id, added

    def pending(self, library_id, limit):
        with self.connection() as db:
            return list(db.execute("""SELECT a.id, a.pdf_url, a.title FROM memberships m
                JOIN articles a ON a.id = m.article_id
                WHERE m.library_id = ? AND a.download_status IN ('pending', 'failed')
                AND a.pdf_url <> '' AND a.body = '' AND a.retry_at <= ?
                ORDER BY a.retry_at, a.id LIMIT ?""", (library_id, now(), limit)))

    def download_failed(self, article_id, reason):
        with self.connection(write=True) as db:
            row = db.execute("SELECT download_attempts FROM articles WHERE id = ?", (article_id,)).fetchone()
            delay = min(24 * 7, 2 ** min(row[0], 8))
            next_try = (datetime.now(timezone.utc) + timedelta(hours=delay)).isoformat()
            db.execute("""UPDATE articles SET download_status = 'failed', download_error = ?,
                retry_at = ?, download_attempts = download_attempts + 1 WHERE id = ? AND body = ''""",
                (reason, next_try, article_id))

    def attach(self, article_id, format_name, data, text, *, only_missing=False, error=""):
        with self.connection(write=True) as db:
            row = db.execute("SELECT body FROM articles WHERE id = ?", (article_id,)).fetchone()
            if not row or (only_missing and row[0]):
                return False
            db.execute("INSERT OR REPLACE INTO attachments VALUES (?, ?, ?)", (article_id, format_name, data))
            status = "downloaded" if text else "no_text"
            db.execute("""UPDATE articles SET body = ?, body_format = ?, download_status = ?,
                download_error = ?, retry_at = '' WHERE id = ?""", (text, format_name, status, error, article_id))
        return True

    def attachment(self, article_id, format_name):
        with self.connection() as db:
            row = db.execute("SELECT data FROM attachments WHERE article_id = ? AND format = ?",
                             (article_id, format_name)).fetchone()
            return row[0] if row else None

    def article(self, article_id):
        with self.connection() as db:
            return db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()

    def external_id(self, article_id, origin):
        prefix = origin + ":"
        with self.connection() as db:
            row = db.execute("""SELECT key FROM article_keys
                WHERE article_id = ? AND substr(key, 1, ?) = ? ORDER BY key LIMIT 1""",
                             (article_id, len(prefix), prefix)).fetchone()
        return row["key"][len(prefix):] if row else ""

    def articles(self, library_id, search, limit, offset, *, scope="all"):
        conditions = " FROM memberships m JOIN articles a ON a.id = m.article_id WHERE m.library_id = ?"
        params = [library_id]
        if scope == "full_text":
            conditions += " AND a.body <> ''"
        elif scope == "pdf":
            conditions += " AND EXISTS(SELECT 1 FROM attachments f WHERE f.article_id = a.id AND f.format = 'pdf')"
        if search:
            conditions += " AND instr(a.search_text, ?) > 0"
            params.append(normalize(search))
        query = """SELECT a.id, a.title, a.doi, a.abstract, a.authors, a.venue, a.published,
            a.url, a.license, a.download_status, a.download_error, length(a.body) > 0 AS full_text,
            EXISTS(SELECT 1 FROM attachments f WHERE f.article_id = a.id AND f.format = 'pdf') AS local_pdf,
            (SELECT group_concat(DISTINCT s.source) FROM article_sources s WHERE s.article_id = a.id) AS sources"""
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*)" + conditions, params).fetchone()[0]
            rows = db.execute(query + conditions + " ORDER BY a.published DESC, a.id DESC LIMIT ? OFFSET ?",
                              (*params, limit, offset))
            result = []
            for row in rows:
                article = dict(row)
                article["content_kind"] = content_kind(article.pop("full_text"), article["abstract"])
                article["authors"] = json.loads(article["authors"] or "[]")
                article["sources"] = (article["sources"] or "").split(",") if article["sources"] else []
                article["local_pdf"] = bool(article["local_pdf"])
                result.append(article)
        return result, total

    def export_articles(self, library_id, full_text_only):
        query = """SELECT a.*, (SELECT group_concat(DISTINCT s.source) FROM article_sources s
            WHERE s.article_id = a.id) AS sources, (SELECT group_concat(ml.library_id) FROM memberships ml
            WHERE ml.article_id = a.id) AS library_ids FROM memberships m JOIN articles a ON a.id = m.article_id
            WHERE m.library_id = ?"""
        if full_text_only:
            query += " AND a.body <> ''"
        with self.connection() as db:
            yield from db.execute(query + " ORDER BY a.published DESC, a.id DESC", (library_id,))


def content_kind(has_body, abstract):
    if has_body:
        return "full_text"
    return "abstract" if abstract else "metadata"
