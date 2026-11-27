"""Library files on disk, outside the database, so their size is limited only by the disk."""

import hashlib
import os
import re
import uuid
from pathlib import Path


class FileStore:
    """One folder of files named by the SHA-256 of their content; the same content is stored once."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, chunks):
        """Write a stream of byte chunks to the store and return its digest."""
        digest = hashlib.sha256()
        incoming = self.root / f".incoming-{uuid.uuid4().hex}"
        try:
            with incoming.open("wb") as target:
                for chunk in chunks:
                    digest.update(chunk)
                    target.write(chunk)
            name = digest.hexdigest()
            if not (self.root / name).exists():
                os.replace(incoming, self.root / name)
        finally:
            incoming.unlink(missing_ok=True)
        return name

    def path(self, digest):
        """Where the file with this digest lives."""
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Недопустимый идентификатор файла.")
        return self.root / digest
