"""Readable, recoverable on-disk projections of the catalog's full texts."""

import json
import os
import re
import shutil
import uuid

from sources import allows_abstract_export


FIGURE = re.compile(r"/api/files/([0-9a-f]{64})\.([a-zA-Z0-9]{1,8})")
ORIGINAL = re.compile(r"source\.(?:pdf|djvu|xml|md|txt)")
PICTURE = re.compile(r"[0-9a-f]{64}\.[a-z0-9]{1,8}")


def relative_folder(article):
    match = re.match(r"^(\d{4})", article["published"])
    def slug(value, limit):
        return re.sub(r"[\W_]+", "-", value.casefold()).strip("-")[:limit].rstrip("-")
    authors = json.loads(article["authors"] or "[]")
    author = slug(authors[0], 22) if authors else ""
    title = slug(article["title"], 44) or "article"
    name = "-".join(filter(None, (str(article["id"]), author, title)))
    return f"{match[1] if match else 'undated'}/{name}"


def write_text(path, text):
    data = text.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return
    incoming = path.with_name(".incoming-" + uuid.uuid4().hex)
    try:
        incoming.write_bytes(data)
        os.replace(incoming, path)
    finally:
        incoming.unlink(missing_ok=True)


def link_file(source, target):
    if target.exists() and source.samefile(target):
        return
    incoming = target.with_name(".incoming-" + uuid.uuid4().hex)
    try:
        try:
            os.link(source, incoming)
        except OSError:
            shutil.copyfile(source, incoming)
        os.replace(incoming, target)
    finally:
        incoming.unlink(missing_ok=True)


def prune_files(directory, managed, keep=()):
    if directory.exists():
        for item in directory.iterdir():
            if item.name not in keep and managed.fullmatch(item.name) and item.is_file():
                item.unlink()


def clean_folder(directory):
    """Keep user notes and other files alongside the managed documents."""
    for name, pattern in (("original", ORIGINAL), ("figures", PICTURE),
                          ("text", re.compile(r"(?:article|references)\.md")),
                          ("metadata", re.compile(r"article\.json"))):
        child = directory / name
        if child.exists():
            prune_files(child, pattern)
            if not any(child.iterdir()):
                child.rmdir()
    if directory.exists() and not any(directory.iterdir()):
        directory.rmdir()


def project(db, files, root, article_id):
    article = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    old = db.execute("SELECT folder FROM article_folders WHERE article_id=?", (article_id,)).fetchone()
    originals = dict(db.execute("SELECT format,digest FROM attachments WHERE article_id=?", (article_id,)))
    folder = relative_folder(article) if article and (originals or article["body"]) else None
    if folder:
        directory = root / folder
        for part in ("original", "text", "figures", "metadata"):
            (directory / part).mkdir(parents=True, exist_ok=True)
        original_names = []
        for fmt, digest in originals.items():
            name = f"source.{fmt}"
            if not ORIGINAL.fullmatch(name):
                raise ValueError(f"Неизвестный формат вложения: {fmt}")
            link_file(files.path(digest), directory / "original" / name)
            original_names.append(name)
        prune_files(directory / "original", ORIGINAL, original_names)
        assets = list(db.execute("SELECT digest,extension FROM article_figures WHERE article_id=?", (article_id,)))
        figure_names = {f"{digest}.{extension}" for digest, extension in assets}
        for digest, extension in assets:
            link_file(files.path(digest), directory / "figures" / f"{digest}.{extension}")
        prune_files(directory / "figures", PICTURE, figure_names)
        ref = db.execute("SELECT text FROM article_references WHERE article_id=?", (article_id,)).fetchone()
        for name, document in (("article.md", article["body"]), ("references.md", ref[0] if ref else "")):
            path = directory / "text" / name
            if document:
                def relative_link(match):
                    name = f"{match[1]}.{match[2].lower()}"
                    return "../figures/" + name if name in figure_names else match[0]
                write_text(path, FIGURE.sub(relative_link, document))
            else:
                path.unlink(missing_ok=True)
        meta = {field: article[field] for field in (
            "id", "title", "doi", "published", "venue", "url", "pdf_url", "license", "abstract", "body_format")}
        meta["authors"] = json.loads(article["authors"] or "[]")
        if not allows_abstract_export(article["abstract_source"]):
            meta.pop("abstract", None)
        meta["sources"] = [dict(row) for row in db.execute(
            "SELECT source,origin,url FROM article_sources WHERE article_id=? ORDER BY origin", (article_id,))]
        meta["library_ids"] = [r[0] for r in db.execute(
            "SELECT library_id FROM visible_memberships WHERE article_id=? ORDER BY library_id", (article_id,))]
        meta["originals"] = originals
        write_text(directory / "metadata" / "article.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        db.execute("INSERT OR REPLACE INTO article_folders VALUES (?,?)", (article_id, folder))
    else:
        db.execute("DELETE FROM article_folders WHERE article_id=?", (article_id,))
    if old and old[0] != folder:
        clean_folder(root / old[0])
