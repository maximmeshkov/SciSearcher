import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from files import FileStore
from article_folders import FIGURE, project
from sources import allows_abstract_export
from sources.common import normalize_doi


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


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
    return bool(text), allows_abstract_export(source), len(text)


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
CREATE INDEX IF NOT EXISTS memberships_article ON memberships(article_id,library_id);
-- Scope 0 is the whole catalog. Keep the article and all its aliases as the tombstone on purge.
CREATE TABLE IF NOT EXISTS exclusions (
    library_id INTEGER NOT NULL,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    PRIMARY KEY(library_id,article_id)
);
CREATE VIEW IF NOT EXISTS visible_memberships AS
    SELECT m.library_id,m.article_id FROM memberships m WHERE NOT EXISTS
        (SELECT 1 FROM exclusions e WHERE e.article_id=m.article_id AND e.library_id IN (0,m.library_id));
CREATE TABLE IF NOT EXISTS article_figures (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    digest TEXT NOT NULL,
    extension TEXT NOT NULL,
    PRIMARY KEY(article_id,digest,extension)
);
CREATE TABLE IF NOT EXISTS article_folders (article_id INTEGER PRIMARY KEY, folder TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS folder_jobs (article_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS unused_file_jobs (digest TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS attachments (
    article_id INTEGER NOT NULL REFERENCES articles(id),
    format TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (article_id, format)
);
CREATE TABLE IF NOT EXISTS parse_queue (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mineru_tasks (
    article_id INTEGER NOT NULL REFERENCES parse_queue(article_id) ON DELETE CASCADE ON UPDATE CASCADE,
    digest TEXT NOT NULL, server TEXT NOT NULL, tier TEXT NOT NULL,
    task_id TEXT NOT NULL, created_at INTEGER NOT NULL,
    PRIMARY KEY(article_id,digest,server,tier)
);
CREATE TABLE IF NOT EXISTS article_references (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    text TEXT NOT NULL
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


# Formats of the article's own document file: MinerU turns them into text.
LOCAL_FILE = "f.format IN ('pdf', 'djvu')"


class Catalog:
    def __init__(self, path, files=None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.files = files or FileStore(path.parent / "files")
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            if "data" in {row[1] for row in db.execute("PRAGMA table_info(attachments)")}:
                # Older versions kept files inside the database; _move_blobs_to_files takes them out.
                db.execute("ALTER TABLE attachments RENAME TO attachments_blob")
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO catalog_metadata VALUES ('identity', ?)", (str(uuid.uuid4()),))
            self.identity = db.execute("SELECT value FROM catalog_metadata WHERE key = 'identity'").fetchone()[0]
            # Merges can delete the highest article ID; never allocate it again.
            db.execute("""INSERT OR IGNORE INTO catalog_metadata
                SELECT 'last_article_id', COALESCE(MAX(id), 0) FROM articles""")
            db.execute("UPDATE runs SET status = 'queued' WHERE status = 'running'")
        self._move_blobs_to_files()
        self.folders = path.parent / "articles"
        with self.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM catalog_metadata WHERE key='article_folders_v2'").fetchone():
                for row in db.execute("""SELECT a.id,a.body,r.text FROM articles a
                        LEFT JOIN article_references r ON r.article_id=a.id WHERE a.body <> '' OR r.text <> ''"""):
                    for digest, ext in FIGURE.findall(row["body"] + "\n" + (row["text"] or "")):
                        if self.files.path(digest).is_file():
                            db.execute("INSERT OR IGNORE INTO article_figures VALUES (?,?,?)",
                                       (row["id"], digest, ext.lower()))
                db.execute("""INSERT OR IGNORE INTO folder_jobs
                    SELECT id FROM articles WHERE body<>'' UNION SELECT article_id FROM attachments""")
                db.execute("INSERT INTO catalog_metadata VALUES ('article_folders_v2','1')")
        self._sync_files()

    def _sync_files(self):
        """The queue is committed with the source mutation. Interrupted projections resume on startup."""
        while True:
            with self.connection(write=True) as db:
                job = db.execute("SELECT article_id FROM folder_jobs ORDER BY article_id LIMIT 1").fetchone()
                if not job:
                    break
                project(db, self.files, self.folders, job[0])
                db.execute("DELETE FROM folder_jobs WHERE article_id=?", (job[0],))
        with self.connection(write=True) as db:
            for row in db.execute("SELECT digest FROM unused_file_jobs").fetchall():
                used = db.execute("""SELECT 1 FROM attachments WHERE digest=? UNION ALL
                    SELECT 1 FROM article_figures WHERE digest=? LIMIT 1""", (row[0],row[0])).fetchone()
                if not used:
                    self.files.path(row[0]).unlink(missing_ok=True)
                db.execute("DELETE FROM unused_file_jobs WHERE digest=?", (row[0],))

    @staticmethod
    def _mark_folder(db, article_id):
        db.execute("INSERT OR IGNORE INTO folder_jobs VALUES (?)", (article_id,))

    @staticmethod
    def _collect_files(db, article_id):
        db.execute("""INSERT OR IGNORE INTO unused_file_jobs SELECT digest FROM attachments WHERE article_id=?
            UNION SELECT digest FROM article_figures WHERE article_id=?""", (article_id,article_id))

    def _move_blobs_to_files(self):
        """One-time move of files that older versions kept inside the database into the file store."""
        with self.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'attachments_blob'").fetchone():
                return
            for article_id, format_name, data in db.execute("SELECT article_id, format, data FROM attachments_blob"):
                db.execute("INSERT OR REPLACE INTO attachments VALUES (?, ?, ?)",
                           (article_id, format_name, self.files.save([data])))
            db.execute("DROP TABLE attachments_blob")

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
        query = f"""SELECT l.*,
            (SELECT COUNT(*) FROM visible_memberships m WHERE m.library_id = l.id) AS article_count,
            (SELECT COUNT(*) FROM visible_memberships m JOIN articles a ON a.id = m.article_id
                WHERE m.library_id = l.id AND a.body <> '') AS full_text_count,
            (SELECT COUNT(*) FROM visible_memberships m JOIN articles a ON a.id=m.article_id
                WHERE m.library_id=l.id AND a.body='' AND NOT EXISTS
                (SELECT 1 FROM attachments f WHERE f.article_id=a.id AND {LOCAL_FILE})) AS no_pdf_count,
            (SELECT COUNT(*) FROM memberships m WHERE m.library_id=l.id AND EXISTS
                (SELECT 1 FROM exclusions e WHERE e.article_id=m.article_id AND e.library_id IN (0,l.id))) AS excluded_count,
            (SELECT COUNT(DISTINCT m.article_id) FROM visible_memberships m JOIN attachments f ON f.article_id = m.article_id
                WHERE m.library_id = l.id AND {LOCAL_FILE}) AS pdf_count,
            (SELECT COUNT(*) FROM visible_memberships m JOIN parse_queue q ON q.article_id = m.article_id
                WHERE m.library_id = l.id AND q.status IN ('pending','parsing')) AS parse_waiting,
            (SELECT COUNT(*) FROM visible_memberships m JOIN parse_queue q ON q.article_id = m.article_id
                WHERE m.library_id = l.id AND q.status = 'failed') AS parse_failed,
            (SELECT started_at FROM runs r WHERE r.library_id = l.id ORDER BY id DESC LIMIT 1) AS last_run_at,
            (SELECT status FROM runs r WHERE r.library_id = l.id ORDER BY id DESC LIMIT 1) AS last_status
            FROM libraries l"""
        params = ()
        if library_id is not None:
            query += " WHERE l.id = ?"
            params = (library_id,)
        with self.connection() as db:
            return [json.loads(row["config"]) | {
                key: row[key] for key in ("id", "article_count", "full_text_count", "pdf_count", "parse_waiting",
                                          "parse_failed", "last_run_at", "last_status","no_pdf_count","excluded_count")
            } | {"metadata_only_count": row["article_count"] - row["full_text_count"]}
                    for row in db.execute(query + " ORDER BY l.id", params)]

    @staticmethod
    def _visible(db, article_id, library_id=None):
        sql = "SELECT 1 FROM visible_memberships WHERE article_id=?"
        params = (article_id,)
        if library_id is not None:
            sql += " AND library_id=?"
            params += (library_id,)
        return bool(db.execute(sql + " LIMIT 1", params).fetchone())

    def visible(self, article_id, library_id=None):
        with self.connection() as db:
            return self._visible(db, article_id, library_id)

    @staticmethod
    def _check_membership(db, article_id, library_id):
        if not db.execute("SELECT 1 FROM memberships WHERE article_id=? AND library_id=?",
                          (article_id, library_id)).fetchone():
            raise KeyError("Статья не найдена в этой библиотеке.")

    def exclude(self, library_id, article_id, *, global_scope=False, purge=False):
        scope = 0 if global_scope else library_id
        with self.connection(write=True) as db:
            self._check_membership(db, article_id, library_id)
            if purge and db.execute("""SELECT 1 FROM visible_memberships WHERE article_id=?
                    AND library_id<>? LIMIT 1""", (article_id, library_id)).fetchone():
                raise ValueError("Файлы используются другой библиотекой. Сначала исключите статью без удаления файлов.")
            db.execute("INSERT OR IGNORE INTO exclusions VALUES (?,?)", (scope,article_id))
            if purge:
                self._collect_files(db, article_id)
                for table in ("attachments","article_references","article_figures","parse_queue"):
                    db.execute(f"DELETE FROM {table} WHERE article_id=?", (article_id,))
                db.execute("""UPDATE articles SET body='',body_format='',download_status='unavailable',
                    download_error='',retry_at='',download_attempts=0 WHERE id=?""", (article_id,))
            self._mark_folder(db, article_id)
        self._sync_files()
        return {"article_id": article_id, "global_scope": global_scope, "purged": purge}

    def restore(self, library_id, article_id, *, global_scope=False):
        scope = 0 if global_scope else library_id
        with self.connection(write=True) as db:
            self._check_membership(db, article_id, library_id)
            was_active = self._visible(db, article_id)
            db.execute("DELETE FROM exclusions WHERE library_id=? AND article_id=?", (scope,article_id))
            if self._visible(db, article_id):
                db.execute("""UPDATE articles SET download_status='pending',retry_at=? WHERE id=?
                    AND download_status='unavailable' AND body='' AND pdf_url<>''""", (now(),article_id))
                if not was_active:
                    db.execute("UPDATE parse_queue SET status='pending' WHERE article_id=? AND status='parsing'",
                               (article_id,))
            self._mark_folder(db, article_id)
        self._sync_files()
        return {"article_id": article_id, "visible": self.visible(article_id,library_id)}

    def save_library(self, config, library_id=None):
        # A library without sources is filled by hand: the scheduler has nothing to run for it.
        scheduled = config["enabled"] and bool(config.get("sources"))
        with self.connection(write=True) as db:
            values = (json.dumps(config, ensure_ascii=False), scheduled, now())
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
                    AND a.digest <> b.digest LIMIT 1""", (hit["id"], *ids)).fetchone()
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
                SELECT ?, format, digest FROM attachments WHERE article_id = ?""", (winner_id, duplicate_id))
            db.execute("""INSERT OR IGNORE INTO article_figures
                SELECT ?,digest,extension FROM article_figures WHERE article_id=?""", (winner_id, duplicate_id))
            db.execute("""INSERT OR IGNORE INTO exclusions
                SELECT library_id,? FROM exclusions WHERE article_id=?""", (winner_id, duplicate_id))
            db.execute("DELETE FROM exclusions WHERE article_id=?", (duplicate_id,))
            db.execute("DELETE FROM article_figures WHERE article_id=?", (duplicate_id,))
            self._mark_folder(db, duplicate_id)
            for table in ("parse_queue", "article_references"):
                db.execute(f"UPDATE OR IGNORE {table} SET article_id = ? WHERE article_id = ?", (winner_id, duplicate_id))
            db.execute("UPDATE OR IGNORE mineru_tasks SET article_id=? WHERE article_id=?", (winner_id, duplicate_id))
            db.execute("DELETE FROM article_sources WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM memberships WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM attachments WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM parse_queue WHERE article_id = ?", (duplicate_id,))
            db.execute("DELETE FROM article_references WHERE article_id = ?", (duplicate_id,))
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
            visible = self._visible(db, article_id, library_id)
            has_files = len(matches) > 1 or (article and (article["body"] or db.execute(
                "SELECT 1 FROM attachments WHERE article_id=? LIMIT 1", (article_id,)).fetchone()))
            if has_files:
                self._mark_folder(db, article_id)
        if has_files:
            self._sync_files()
        return (article_id, added) if visible else (None, 0)

    def pending(self, library_id, limit):
        with self.connection() as db:
            return list(db.execute(f"""SELECT a.id, a.pdf_url, a.doi, a.title FROM visible_memberships m
                JOIN articles a ON a.id = m.article_id
                WHERE m.library_id = ? AND a.download_status IN ('pending', 'failed')
                AND a.pdf_url <> '' AND a.body = '' AND a.retry_at <= ?
                AND NOT EXISTS(SELECT 1 FROM attachments f WHERE f.article_id=a.id AND {LOCAL_FILE})
                ORDER BY a.retry_at, a.id LIMIT ?""", (library_id, now(), limit)))

    def download_failed(self, article_id, reason):
        with self.connection(write=True) as db:
            row = db.execute("SELECT download_attempts FROM articles WHERE id = ?", (article_id,)).fetchone()
            if not row or not self._visible(db, article_id):
                return
            delay = min(24 * 7, 2 ** min(row[0], 8))
            next_try = (datetime.now(timezone.utc) + timedelta(hours=delay)).isoformat()
            db.execute("""UPDATE articles SET download_status = 'failed', download_error = ?,
                retry_at = ?, download_attempts = download_attempts + 1 WHERE id = ? AND body = ''""",
                (reason, next_try, article_id))

    def attach(self, article_id, format_name, digest, text, *, only_missing=False, error="", provenance=None,
               staged=None):
        """Link a stored file (by digest) to the article and set the article text from it."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT body,body_format FROM articles WHERE id = ?", (article_id,)).fetchone()
            if not row or not self._visible(db, article_id):
                return False
            if only_missing and (row[0] or db.execute(
                    f"SELECT 1 FROM attachments f WHERE article_id=? AND {LOCAL_FILE}", (article_id,)).fetchone()):
                return False
            if staged is not None:
                self.files.publish(digest, staged)
            elif not self.files.path(digest).is_file():
                raise FileNotFoundError("Файл уже удалён. Повторите загрузку.")
            self._collect_files(db, article_id)
            db.execute("INSERT OR REPLACE INTO attachments VALUES (?, ?, ?)", (article_id, format_name, digest))
            if format_name in ("pdf", "djvu"):
                db.execute("""DELETE FROM mineru_tasks WHERE article_id=? AND digest NOT IN
                    (SELECT digest FROM attachments f WHERE f.article_id=? AND f.format IN ('pdf','djvu'))""",
                           (article_id, article_id))
            if provenance is not None:
                db.execute("UPDATE articles SET pdf_url=?,license=? WHERE id=?", (*provenance, article_id))
            if not (format_name in ("pdf", "djvu") and row["body"] and row["body_format"] in ("md", "xml", "txt")):
                db.execute("DELETE FROM article_figures WHERE article_id=?", (article_id,))
                db.execute("DELETE FROM article_references WHERE article_id=?", (article_id,))
                status = "downloaded" if text else "no_text"
                body_format = "pdf_no_text" if format_name == "pdf" and not text and error else format_name
                db.execute("""UPDATE articles SET body = ?, body_format = ?, download_status = ?,
                    download_error = ?, retry_at = '' WHERE id = ?""", (text, body_format, status, error, article_id))
            self._mark_folder(db, article_id)
        self._sync_files()
        return True

    def queue_parse(self, article_id):
        """Put the article's PDF in the parse queue; a new copy starts the attempts over."""
        with self.connection(write=True) as db:
            if not self._visible(db, article_id):
                return
            db.execute("""INSERT INTO parse_queue (article_id, status, retry_at) VALUES (?, 'pending', ?)
                ON CONFLICT (article_id) DO UPDATE SET status = 'pending', error = '', attempts = 0,
                retry_at = excluded.retry_at""", (article_id, now()))

    def queue_unparsed_pdfs(self, *, mineru=True):
        """Prepare the active parser; paused remote checkpoints survive a period without MinerU."""
        with self.connection(write=True) as db:
            if not mineru:
                db.execute("UPDATE parse_queue SET status='paused' WHERE status<>'paused'")
                return
            db.execute("""DELETE FROM parse_queue WHERE status='paused' AND article_id IN
                (SELECT id FROM articles WHERE body_format IN ('mineru','xml','md','txt'))""")
            db.execute("UPDATE parse_queue SET status = 'pending' WHERE status IN ('parsing','paused')")
            return db.execute(f"""INSERT OR IGNORE INTO parse_queue (article_id, status, retry_at)
                SELECT DISTINCT f.article_id, 'pending', ? FROM attachments f JOIN articles a ON a.id = f.article_id
                WHERE {LOCAL_FILE} AND a.body_format NOT IN ('mineru', 'xml', 'md', 'txt')
                AND EXISTS(SELECT 1 FROM visible_memberships m WHERE m.article_id=a.id)""",
                              (now(),)).rowcount

    def next_pypdf(self):
        with self.connection() as db:
            return db.execute("""SELECT a.id,f.digest FROM attachments f JOIN articles a ON a.id=f.article_id
                WHERE f.format='pdf' AND a.body='' AND a.body_format NOT IN ('pdf_no_text','xml','md','txt','mineru')
                AND (a.download_status<>'no_text' OR a.retry_at<=?)
                AND EXISTS(SELECT 1 FROM visible_memberships m WHERE m.article_id=a.id)
                ORDER BY a.id LIMIT 1""", (now(),)).fetchone()

    def save_pypdf(self, article_id, digest, text, error="", *, retry_read=False):
        """Only update the same visible, still-unread original that was opened for extraction."""
        with self.connection(write=True) as db:
            if not self._same_input(db, article_id, digest) or not self._visible(db, article_id):
                return
            fmt = "pdf" if text or retry_read else "pdf_no_text"
            retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat() if retry_read else ""
            updated = db.execute("""UPDATE articles SET body=?,body_format=?,download_status=?,download_error=?,retry_at=?
                WHERE id=? AND body='' AND body_format NOT IN ('xml','md','txt','mineru')""",
                (text, fmt, "downloaded" if text else "no_text", error, retry_at, article_id)).rowcount
            if updated and not retry_read:
                self._mark_folder(db, article_id)
        if updated and not retry_read:
            self._sync_files()

    def next_parse(self, *, server="", tier=""):
        """Take the oldest due article from the parse queue: (article_id, format, digest) or None.

        An article with both a PDF and a DjVu file is parsed from the PDF.
        """
        with self.connection(write=True) as db:
            row = db.execute(f"""SELECT q.article_id, f.format, f.digest FROM parse_queue q JOIN attachments f
                ON f.article_id = q.article_id AND {LOCAL_FILE} JOIN articles a ON a.id=q.article_id
                WHERE q.status IN ('pending', 'failed') AND q.retry_at <= ?
                AND EXISTS(SELECT 1 FROM visible_memberships m WHERE m.article_id=q.article_id)
                ORDER BY EXISTS(SELECT 1 FROM mineru_tasks t WHERE t.article_id=q.article_id AND
                    t.digest=f.digest AND t.server=? AND t.tier=?) DESC,
                    (a.body <> ''), q.retry_at, q.article_id, f.format DESC LIMIT 1""",
                (now(), server, tier)).fetchone()
            if row:
                db.execute("UPDATE parse_queue SET status = 'parsing' WHERE article_id = ?", (row[0],))
        return tuple(row) if row else None

    @staticmethod
    def _same_input(db, article_id, digest):
        row = db.execute("""SELECT digest FROM attachments WHERE article_id=? AND format IN ('pdf','djvu')
            ORDER BY format DESC LIMIT 1""", (article_id,)).fetchone()
        return row is not None and row[0] == digest

    def mineru_task(self, key, *, created=None):
        """Persist the remote ID before any upload. Concurrent claims agree on a single registered task."""
        article_id, digest, server, tier = key
        with self.connection(write=created is not None) as db:
            if created is not None:
                if not (self._same_input(db, article_id, digest) and self._visible(db, article_id)
                        and db.execute("SELECT 1 FROM parse_queue WHERE article_id=?", (article_id,)).fetchone()):
                    return None
                db.execute("INSERT OR IGNORE INTO mineru_tasks VALUES (?,?,?,?,?,?)", (*key, *created))
            row = db.execute("""SELECT task_id,created_at FROM mineru_tasks
                WHERE article_id=? AND digest=? AND server=? AND tier=?""", (article_id, digest, server, tier)).fetchone()
            return tuple(row) if row else None

    def forget_mineru_task(self, key, task_id):
        with self.connection(write=True) as db:
            db.execute("""DELETE FROM mineru_tasks
                WHERE article_id=? AND digest=? AND server=? AND tier=? AND task_id=?""", (*key, task_id))

    def resume_parse_later(self, article_id, digest):
        """Requeue an interrupted claim without erasing its task ID, backoff history or a newer claim."""
        with self.connection(write=True) as db:
            if self._same_input(db, article_id, digest):
                db.execute("UPDATE parse_queue SET status='pending',retry_at=? WHERE article_id=? AND status='parsing'",
                           (now(), article_id))

    def save_parsed(self, article_id, prepared, *, expected_digest=None):
        """Store prepared text and references and take the article off the parse queue.

        Text the user supplied as XML, Markdown or TXT is kept: it is better than a PDF parse.
        Pictures the text links to go to the file store first, so a stored link never points nowhere.
        """
        with self.connection(write=True) as db:
            if expected_digest and not self._same_input(db, article_id, expected_digest):
                return False
            if not self._visible(db, article_id):
                # Resume a hidden parse if the user restores the article later.
                db.execute("UPDATE parse_queue SET status='pending' WHERE article_id=?", (article_id,))
                return False
            self._collect_files(db, article_id)
            db.execute("DELETE FROM article_figures WHERE article_id=?", (article_id,))
            extensions = dict(FIGURE.findall(prepared.body + "\n" + prepared.references))
            for key, picture in prepared.figures.items():
                digest = self.files.save([picture])
                ext = extensions.get(key, "png" if picture.startswith(b"\x89PNG") else "jpg").lower()
                db.execute("INSERT OR IGNORE INTO article_figures VALUES (?,?,?)", (article_id,digest,ext))
            db.execute("""UPDATE articles SET body = ?, body_format = 'mineru', download_status = 'downloaded',
                download_error = '', retry_at = '' WHERE id = ? AND body_format NOT IN ('xml', 'md', 'txt')""",
                       (prepared.body, article_id))
            if db.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone():
                db.execute("INSERT OR REPLACE INTO article_references VALUES (?, ?)", (article_id, prepared.references))
            db.execute("DELETE FROM parse_queue WHERE article_id = ?", (article_id,))
            self._mark_folder(db, article_id)
        self._sync_files()
        return True

    def parse_failed(self, article_id, reason, *, expected_digest=None):
        """Keep the article in the parse queue and retry later with a growing pause."""
        with self.connection(write=True) as db:
            if expected_digest and not self._same_input(db, article_id, expected_digest):
                return
            row = db.execute("SELECT attempts FROM parse_queue WHERE article_id = ?", (article_id,)).fetchone()
            if row is None:
                return
            delay = min(24 * 7, 2 ** min(row[0], 8))
            next_try = (datetime.now(timezone.utc) + timedelta(hours=delay)).isoformat()
            db.execute("""UPDATE parse_queue SET status = 'failed', error = ?, retry_at = ?,
                attempts = attempts + 1 WHERE article_id = ?""", (reason, next_try, article_id))

    def attachment(self, article_id, format_name):
        """Digest of the article's file in this format, or None."""
        with self.connection() as db:
            row = db.execute("SELECT digest FROM attachments WHERE article_id = ? AND format = ?",
                             (article_id, format_name)).fetchone()
            return row[0] if row else None

    def visible_figure(self, digest, extension):
        with self.connection() as db:
            return bool(db.execute("""SELECT 1 FROM article_figures f JOIN visible_memberships m
                ON m.article_id=f.article_id WHERE f.digest=? AND f.extension=? LIMIT 1""",
                                   (digest, extension)).fetchone())

    def article(self, article_id):
        with self.connection() as db:
            return db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()

    def source_ids(self, article_id, kind):
        """Look up the actual IDs, including IDs scoped by a configured feed or server URL."""
        with self.connection() as db:
            return [row[0] for row in db.execute("""SELECT DISTINCT substr(k.key,length(s.origin)+2) AS local_id
                FROM article_sources s JOIN article_keys k ON k.article_id=s.article_id
                WHERE s.article_id=? AND s.source=? AND substr(k.key,1,length(s.origin)+1)=s.origin||':'
                ORDER BY local_id""", (article_id, kind))]

    def external_id(self, article_id, origin):
        prefix = origin + ":"
        with self.connection() as db:
            row = db.execute("""SELECT key FROM article_keys WHERE article_id=?
                AND substr(key,1,?)=? ORDER BY key LIMIT 1""", (article_id, len(prefix), prefix)).fetchone()
            return row[0][len(prefix):] if row else ""

    def articles(self, library_id, search, limit, offset, *, scope="all"):
        membership_table = "memberships" if scope == "excluded" else "visible_memberships"
        conditions = (f" FROM {membership_table} m JOIN articles a ON a.id = m.article_id"
                      " LEFT JOIN parse_queue q ON q.article_id = a.id WHERE m.library_id = ?")
        params = [library_id]
        if scope == "full_text":
            conditions += " AND a.body <> ''"
        elif scope == "pdf":
            conditions += f" AND EXISTS(SELECT 1 FROM attachments f WHERE f.article_id = a.id AND {LOCAL_FILE})"
        elif scope == "metadata_only":
            conditions += " AND a.body=''"
        elif scope == "no_pdf":
            conditions += f" AND a.body='' AND NOT EXISTS(SELECT 1 FROM attachments f WHERE f.article_id=a.id AND {LOCAL_FILE})"
        elif scope == "parse_pending":
            conditions += " AND q.status IN ('pending','parsing')"
        elif scope == "download_failed":
            conditions += " AND a.download_status='failed'"
        elif scope == "parse_failed":
            conditions += " AND q.status='failed'"
        elif scope == "excluded":
            conditions += " AND EXISTS(SELECT 1 FROM exclusions e WHERE e.article_id=a.id AND e.library_id IN (0,m.library_id))"
        if search:
            conditions += " AND instr(a.search_text, ?) > 0"
            params.append(normalize(search))
        query = f"""SELECT a.id, a.title, a.doi, a.abstract, a.authors, a.venue, a.published,
            a.url, a.license, a.download_status, a.download_error, length(a.body) > 0 AS full_text,
            EXISTS(SELECT 1 FROM attachments f WHERE f.article_id = a.id AND {LOCAL_FILE}) AS local_pdf,
            (SELECT group_concat(DISTINCT s.source) FROM article_sources s WHERE s.article_id = a.id) AS sources,
            (SELECT folder FROM article_folders WHERE article_id=a.id) AS folder,
            EXISTS(SELECT 1 FROM exclusions e WHERE e.article_id=a.id AND e.library_id=0) AS global_excluded,
            EXISTS(SELECT 1 FROM exclusions e WHERE e.article_id=a.id AND e.library_id=m.library_id) AS library_excluded,
            q.status AS parse_status, q.error AS parse_error"""
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
                if article["folder"]:
                    article["folder"] = "articles/" + article["folder"]
                result.append(article)
        return result, total

    def export_articles(self, library_id, full_text_only):
        query = """SELECT a.*, (SELECT group_concat(DISTINCT s.source) FROM article_sources s
            WHERE s.article_id = a.id) AS sources, (SELECT group_concat(ml.library_id) FROM visible_memberships ml
            WHERE ml.article_id = a.id) AS library_ids FROM visible_memberships m JOIN articles a ON a.id = m.article_id
            WHERE m.library_id = ?"""
        if full_text_only:
            query += " AND a.body <> ''"
        with self.connection() as db:
            yield from db.execute(query + " ORDER BY a.published DESC, a.id DESC", (library_id,))


def content_kind(has_body, abstract):
    if has_body:
        return "full_text"
    return "abstract" if abstract else "metadata"
