"""Client for a MinerU parsing server (mineru-kit api-server) on the local network.

The address comes from the operator's configuration, not from article data, so it may be
private (a Docker service name or a LAN host). That is why this client does not use the
public-only downloader in sources/net.py.
"""

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import quote


class MinerU:
    """One MinerU API server: base URL, optional API key and parse tier."""

    def __init__(self, url, api_key="", tier="", *, poll_seconds=5.0, timeout_seconds=48 * 3600):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.tier = tier
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def parse(self, path, file_name):
        """Send one PDF from disk; returns MinerU's middle JSON and the pictures it cut out (name → bytes)."""
        path = Path(path)
        task_id, upload = self.create_task(file_name, path.stat().st_size)
        self.upload(upload, path)
        return result_parts(self.download(self.wait(task_id)))

    def create_task(self, file_name, size):
        """Register one file; returns (task_id, upload link)."""
        request = {"output_formats": ["default"], "files": [{"file_name": file_name, "bytes": size}]}
        if self.tier:
            request["tier"] = self.tier
        task = json.loads(self._call("POST", f"{self.url}/v1/tasks", json.dumps(request).encode(),
                                     {"Content-Type": "application/json"}))
        return task["task_id"], task["files"][0]["upload"]

    def upload(self, upload, path):
        """Stream the file from disk to the link MinerU issued."""
        headers = dict(upload.get("headers") or {"Content-Type": "application/octet-stream"})
        headers["Content-Length"] = str(path.stat().st_size)
        with path.open("rb") as body:
            self._call("PUT", upload["url"], body, headers)

    def wait(self, task_id):
        """Poll the task until its file is parsed; returns the result URL."""
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            task = json.loads(self._call("GET", f"{self.url}/v1/tasks/{quote(task_id, safe='')}"))
            item = task["files"][0]
            if item["status"] == "completed" and item.get("result_url"):
                return item["result_url"]
            if item["status"] in ("failed", "canceled") or task["status"] in ("failed", "canceled"):
                error = item.get("error") or task.get("error") or {}
                raise ValueError(f"MinerU: {error.get('message') or item['status']}")
            if time.monotonic() > deadline:
                self._call("POST", f"{self.url}/v1/tasks/{quote(task_id, safe='')}/cancel")
                raise TimeoutError("MinerU не успел разобрать файл за отведённое время.")
            time.sleep(self.poll_seconds)

    def download(self, result_url):
        """Fetch the parse result archive."""
        return self._call("GET", result_url)

    def _call(self, method, url, body=None, headers=None):
        headers = dict(headers or {})
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(300).decode("utf-8", "replace")
            raise ValueError(f"MinerU: HTTP {error.code} {detail}") from None
        except (urllib.error.URLError, OSError) as error:
            # The server is down or restarting: not the article's fault, so callers retry without penalty.
            raise ConnectionError(f"MinerU недоступен: {getattr(error, 'reason', error)}") from None


def result_parts(result):
    """Split a MinerU result into (middle JSON, pictures by image path).

    The result is a zip archive with the JSON and an images/ folder, or the bare JSON without pictures.
    """
    if result[:2] != b"PK":
        data = json.loads(result)
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            raise ValueError("В ответе MinerU нет разбора страниц.")
        return data, {}
    middle = None
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        images = {name: archive.read(name) for name in archive.namelist()
                  if name.startswith("images/") and not name.endswith("/")}
        for name in archive.namelist():
            if middle is None and name.endswith(".json"):
                data = json.loads(archive.read(name))
                if isinstance(data, dict) and isinstance(data.get("pages"), list):
                    middle = data
    if middle is None:
        raise ValueError("В ответе MinerU нет разбора страниц.")
    return middle, images
