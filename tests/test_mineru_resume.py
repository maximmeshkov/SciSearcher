import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Indexer, standardized
from catalog import Catalog
from mineru import MinerU, MinerUHTTPError


MIDDLE = {"pages": [{"page_idx": 0, "blocks": [{"type": "text", "content": [
    {"type": "text", "content": "Load-pull at 10 GHz."}]}]}]}


class Remote:
    """Same service survives fresh Catalog, Indexer and MinerU client instances."""
    def __init__(self):
        self.created = 0
        self.uploads = []
        self.tasks = {}
        self.interrupt = ""
        self.result_status = []
        self.running_polls = 0

    def call(self, method, url, body=None, headers=None):
        if method == "POST" and url.endswith("/v1/tasks"):
            self.created += 1
            task_id = str(self.created)
            self.tasks[task_id] = "waiting-file"
            return json.dumps({"task_id": task_id, "files": [{"upload": {"url": "upload/" + task_id}}]}).encode()
        task_id = url.split("/")[-1]
        if task_id not in self.tasks:
            raise MinerUHTTPError(404, "expired")
        if method == "PUT":
            if self.interrupt == "before upload":
                self.interrupt = ""
                raise ConnectionError("offline")
            self.uploads.append((task_id, body.read()))
            self.tasks[task_id] = "completed"
            if self.interrupt == "after upload":
                self.interrupt = ""
                raise ConnectionError("connection lost after CPU accepted the upload")
            return b"{}"
        if method == "GET" and url.startswith("result/"):
            if self.result_status:
                raise MinerUHTTPError(self.result_status.pop(0), "result link expired")
            return json.dumps(MIDDLE).encode()
        if method == "GET":
            status = self.tasks[task_id]
            if status == "running":
                self.running_polls += 1
                self.tasks[task_id] = "completed"
            return json.dumps({"status": status, "files": [{"status": status, "upload": {"url": "upload/" + task_id},
                                                             "result_url": "result/" + task_id}]}).encode()
        raise AssertionError(method)


class ResumeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "catalog.sqlite3"
        self.remote = Remote()
        self.restart()
        self.lib = self.cat.save_library({"name": "Load-pull", "enabled": False})["id"]
        self.aid = self.cat.add_article(self.lib, standardized({"title": "Load-pull"}, {"kind": "openalex"}))[0]
        self.replace(b"%PDF-original")

    def replace(self, data):
        digest = self.cat.files.save([data])
        self.cat.attach(self.aid, "pdf", digest, "")
        self.cat.queue_parse(self.aid)

    def restart(self, server="http://mineru", tier="basic"):
        self.cat = Catalog(self.path)
        client = MinerU(server, tier=tier, poll_seconds=0)
        client._call = self.remote.call
        self.indexer = Indexer(self.cat, parser=client)
        self.cat.queue_unparsed_pdfs()

    def checkpoint_count(self):
        with self.cat.connection() as db:
            return db.execute("SELECT COUNT(*) FROM mineru_tasks").fetchone()[0]

    def test_due_remote_task_precedes_submitting_another_file(self):
        aid, _ = self.cat.add_article(self.lib, standardized({"title": "Later file"}, {"kind": "openalex"}))
        digest = self.cat.files.save([b"%PDF-later"])
        self.cat.attach(aid, "pdf", digest, "")
        self.cat.queue_parse(aid)
        self.cat.mineru_task((aid, digest, "http://mineru", "basic"), created=("existing", 1))
        self.assertEqual(self.cat.next_parse(server="http://mineru", tier="basic")[0], aid)

    def test_lost_upload_response_then_restart_polls_same_task_without_sending_pdf_again(self):
        self.remote.interrupt = "after upload"
        self.assertFalse(self.indexer.parse_next())
        self.assertEqual(self.checkpoint_count(), 1)
        self.remote.tasks["1"] = "running"
        self.restart()
        self.assertTrue(self.indexer.parse_next())
        self.assertEqual(self.remote.running_polls, 1)
        self.assertIn("10 GHz", self.cat.article(self.aid)["body"])
        self.assertEqual((self.remote.created, self.remote.uploads), (1, [("1", b"%PDF-original")]))
        self.assertEqual(self.checkpoint_count(), 0)

    def test_disconnect_before_upload_reuses_waiting_file_and_crash_before_commit_reuses_result(self):
        self.remote.interrupt = "before upload"
        self.assertFalse(self.indexer.parse_next())
        self.restart()
        with patch.object(self.cat, "save_parsed", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.indexer.parse_next()
        self.restart()
        self.assertTrue(self.indexer.parse_next())
        self.assertEqual((self.remote.created, self.remote.uploads), (1, [("1", b"%PDF-original")]))

    def test_missing_remote_task_issues_one_replacement_and_stale_result_link_retries_same_id(self):
        self.remote.interrupt = "after upload"
        self.assertFalse(self.indexer.parse_next())
        self.remote.tasks.clear()
        self.remote.result_status = [404]
        self.restart()
        self.assertTrue(self.indexer.parse_next())
        self.assertEqual([a for a, _ in self.remote.uploads], ["1", "2"])
        self.assertEqual(self.remote.created, 2)
        self.assertEqual(self.checkpoint_count(), 0)

    def test_replacement_purge_and_profiles_do_not_reuse_unrelated_remote_tasks(self):
        for tag, data, server, tier in (("1", b"%PDF-original", "http://mineru", "basic"),
                ("2", b"%PDF-new", "http://mineru", "basic"), ("3", b"%PDF-new", "http://other", "basic"),
                ("4", b"%PDF-new", "http://other", "standard")):
            if tag == "2":
                self.replace(data)
                self.assertEqual(self.checkpoint_count(), 0)
            self.restart(server, tier)
            self.remote.interrupt = "after upload"
            self.assertFalse(self.indexer.parse_next())
            self.assertEqual(self.remote.uploads[-1], (tag, data))
        self.cat.exclude(self.lib, self.aid, purge=True)
        self.assertEqual(self.checkpoint_count(), 0)
        self.assertFalse(self.indexer.parse_next())

    def test_gone_artifact_is_replaced_once_even_if_service_keeps_returning_broken_results(self):
        self.remote.interrupt = "after upload"
        self.assertFalse(self.indexer.parse_next())
        self.remote.result_status = [410] * 4
        self.restart()
        self.assertTrue(self.indexer.parse_next())
        self.assertEqual(self.remote.created, 2)
        self.assertEqual(self.checkpoint_count(), 0)
        self.assertFalse(self.indexer.parse_next())

    def test_terminal_failure_releases_task_and_preserves_normal_parse_backoff(self):
        self.remote.interrupt = "after upload"
        self.assertFalse(self.indexer.parse_next())
        self.remote.tasks["1"] = "failed"
        self.restart()
        self.assertTrue(self.indexer.parse_next())
        self.assertEqual(self.checkpoint_count(), 0)
        self.assertFalse(self.indexer.parse_next())
        self.cat.queue_parse(self.aid)
        self.assertTrue(self.indexer.parse_next())
        self.assertEqual(self.remote.created, 2)
