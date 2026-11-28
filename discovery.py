"""Persistent second pass over catalog records missing an article file."""

import logging
import threading
import time

from sources import resolved_pdf


MISSING = """a.body = '' AND NOT EXISTS (SELECT 1 FROM attachments f
    WHERE f.article_id=a.id AND f.format IN ('pdf','djvu'))"""


class Discovery:
    def __init__(self, indexer):
        self.indexer = indexer
        self.catalog = indexer.catalog
        self.wake = threading.Event()
        with self.catalog.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS discovery (
                article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
                status TEXT NOT NULL, next_try INTEGER NOT NULL DEFAULT 0,
                requested_by INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '')""")
            db.execute("UPDATE discovery SET status='queued' WHERE status='checking'")

    def queue(self, library_id, article_id=None):
        with self.catalog.connection(write=True) as db:
            items = db.execute(f"""SELECT a.id FROM articles a JOIN visible_memberships m ON m.article_id=a.id
                WHERE m.library_id=? AND {MISSING} AND (? IS NULL OR a.id=?)""",
                (library_id, article_id, article_id)).fetchall()
            if article_id and not items:
                raise ValueError("Запись уже содержит файл или недоступна в этой библиотеке.")
            db.executemany("""INSERT INTO discovery(article_id,status,requested_by,next_try) VALUES (?,'queued',?,?)
                ON CONFLICT(article_id) DO UPDATE SET status='queued',requested_by=excluded.requested_by,
                next_try=excluded.next_try WHERE discovery.status <> 'checking'""",
                [(r[0], library_id, -1 if article_id else 0) for r in items])
        self.wake.set()
        return {"queued": len(items)}

    def status(self, library_id):
        with self.catalog.connection() as db:
            counts = dict(db.execute(f"""SELECT COALESCE(d.status,'unchecked'),COUNT(*) FROM articles a
                JOIN visible_memberships m ON m.article_id=a.id LEFT JOIN discovery d ON d.article_id=a.id
                WHERE m.library_id=? AND {MISSING} GROUP BY COALESCE(d.status,'unchecked')""", (library_id,)))
            counts["recovered"] = db.execute("""SELECT COUNT(*) FROM discovery d JOIN visible_memberships m
                ON m.article_id=d.article_id WHERE d.status='found' AND m.library_id=?""", (library_id,)).fetchone()[0]
            return counts

    def annotate(self, items):
        if not items:
            return
        by_id = {item["id"]: item for item in items}
        slots = ",".join("?" for _ in by_id)
        with self.catalog.connection() as db:
            for row in db.execute(f"SELECT article_id,status,detail,next_try FROM discovery WHERE article_id IN ({slots})", tuple(by_id)):
                by_id[row[0]].update(discovery_status=row[1], discovery_detail=row[2], discovery_next_try=row[3])

    def _claim(self):
        with self.catalog.connection(write=True) as db:
            item = db.execute(f"""SELECT a.id,a.doi,a.pdf_url,a.license,COALESCE(d.attempts,0) AS attempts
                FROM articles a LEFT JOIN discovery d ON d.article_id=a.id
                WHERE {MISSING} AND COALESCE(d.next_try,0)<=? AND COALESCE(d.status,'') <> 'checking'
                AND EXISTS (SELECT 1 FROM visible_memberships m JOIN libraries l ON l.id=m.library_id
                    WHERE m.article_id=a.id AND (d.requested_by=m.library_id OR
                    (l.enabled AND json_extract(l.config,'$.download_open_access') AND
                     a.download_status IN ('unavailable','failed'))))
                ORDER BY (d.requested_by IS NULL), COALESCE(d.next_try,0), a.id LIMIT 1""", (int(time.time()),)).fetchone()
            if item:
                db.execute("""INSERT INTO discovery(article_id,status) VALUES (?,'checking')
                    ON CONFLICT(article_id) DO UPDATE SET status='checking'""", (item["id"],))
            return item

    def _lookup(self, item):
        errors = []
        fallback = (item["pdf_url"], item["license"], item["pdf_url"]) if item["pdf_url"] else None
        found = resolved_pdf(item["doi"], lambda kind: self.catalog.source_ids(item["id"], kind),
                             self.indexer.download, self.indexer.read_pdf, errors, fallback=fallback)
        if found and self.indexer.store_pdf(item["id"], found[0], provenance=found[1]):
            return "found", ""
        return ("failed", " · ".join(errors)[:500]) if errors else ("no_copy", "")

    def run_once(self):
        item = self._claim()
        if not item:
            return False
        try:
            state, detail = self._lookup(item)
        except Exception:
            logging.exception("Ошибка обработки PDF статьи %s", item["id"])
            state, detail = "failed", "Ошибка обработки файла. Подробности в журнале."
        delay = 30 * 86400 if state in ("found","no_copy") else 3600 * min(168, 2**min(item["attempts"],8))
        with self.catalog.connection(write=True) as db:
            db.execute("""UPDATE discovery SET status=?,detail=?,next_try=?,requested_by=NULL,attempts=attempts+1
                WHERE article_id=?""", (state, detail, int(time.time())+delay, item["id"]))
        return True

    def run(self):
        while not self.indexer.stop.is_set():
            self.wake.clear()
            try:
                active = self.run_once()
            except Exception:
                logging.exception("Ошибка очереди поиска PDF")
                active = False
            self.wake.wait(1 if active else 30)
