"""Library files on disk, outside the database, so their size is limited only by the disk."""

import hashlib
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path


class FileStore:
    """One folder of files named by the SHA-256 of their content; the same content is stored once."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def stage(self, chunks):
        """A request owns this temporary copy until its catalog transaction accepts or rejects it."""
        digest = hashlib.sha256()
        incoming = self.root / f".incoming-{uuid.uuid4().hex}"
        try:
            with incoming.open("wb") as target:
                for chunk in chunks:
                    digest.update(chunk)
                    target.write(chunk)
            yield digest.hexdigest(), incoming
        finally:
            incoming.unlink(missing_ok=True)

    def publish(self, digest, staged):
        target = self.path(digest)
        if not target.exists():
            os.replace(staged, target)

    def save(self, chunks):
        """Write a byte stream to the store and return its digest."""
        with self.stage(chunks) as (digest, staged):
            self.publish(digest, staged)
            return digest

    def path(self, digest):
        """Where the file with this digest lives."""
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Недопустимый идентификатор файла.")
        return self.root / digest
