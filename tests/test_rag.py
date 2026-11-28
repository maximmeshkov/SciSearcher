import tempfile
import threading
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from catalog import Catalog
from rag import HttpFailure, Rag, RagUnavailable, clean_settings, defaults, in_window


class Services:
    """Exercise REST requests, point replacement and Qdrant filter semantics without downloading models."""
    def __init__(self):
        self.collections = {}
        self.calls = []
        self.answer = "Измерение выполнено на частоте 5,4 ГГц [1]."
        self.down = set()
        self.on_chat = None

    @staticmethod
    def matches(point, f):
        def check(cond):
            value = point["payload"][cond["key"]]
            return cond["match"]["value"] in value if isinstance(value, list) else cond["match"]["value"] == value
        return all(check(c) for c in f.get("must", [])) and not any(check(c) for c in f.get("must_not", []))

    def __call__(self, method, url, data=None, **kwargs):
        self.calls.append((url, data))
        if url.endswith("/embeddings"):
            if url in self.down:
                raise HttpFailure(503, "offline")
            return {"data": [{"index": i, "embedding": [1.0, 0.5]} for i in range(len(data["input"]))]}
        if url.endswith("/chat/completions"):
            if self.on_chat:
                self.on_chat()
            return {"choices": [{"message": {"content": self.answer}}]}
        if url.endswith("/api/v1/chat"):
            return {"output": [{"type":"message","content":self.answer}]}
        if url.endswith("/models"):
            return {"data": [{"id":"chat"}, {"id":"embed"}]}
        name, _, action = url.split("/collections/")[1].partition("/")
        if method == "PUT" and not action:
            self.collections[name] = {}
            return {"result": True}
        if name not in self.collections:
            raise HttpFailure(404, "missing collection")
        points = self.collections[name]
        if method == "GET":
            return {"result": {"config":{"params":{"vectors":{"distance":"Cosine","size":2}}}}}
        if method == "DELETE":
            del self.collections[name]
        elif action.startswith("points/delete"):
            for key in [key for key, v in points.items() if self.matches(v, data["filter"])]:
                del points[key]
        elif action == "points/query":
            return {"result":{"points":[{**p,"score":0.96} for p in points.values()
                                      if self.matches(p, data["filter"])][:data["limit"]]}}
        elif method == "PUT":
            points.update({p["id"]:p for p in data["points"]})
        else:
            raise AssertionError((method, url, data))
        return {"result":True}


class RagTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.cat = Catalog(Path(temp.name) / "test.sqlite")
        self.a = self.cat.save_library({"name":"Load-pull", "enabled":False})["id"]
        self.b = self.cat.save_library({"name":"Other", "enabled":False})["id"]
        self.rag = Rag(self.cat, "http://qdrant")
        self.services = Services()
        mock = patch("rag.http_json", side_effect=self.services)
        mock.start()
        self.addCleanup(mock.stop)
        p = dict(defaults()["profiles"][0], base_url="http://model/v1", embedding_model="embed", chat_model="chat")
        self.settings = {"profiles":[p], "embedding_model":"embed", "chat_model":"chat"}
        self.rag.save_settings(self.settings)

    def add(self, library, title, body=""):
        paper = dict(title=title, doi="10.1/" + title, authors=[], abstract="", venue="", published="",
                     url="", pdf_url="", license="", external_id=title, source="rss", origin="rss")
        article_id = self.cat.add_article(library, paper)[0]
        if body:
            with self.cat.connection(write=True) as db:
                db.execute("UPDATE articles SET body=?,body_format='md' WHERE id=?", (body, article_id))
        return article_id

    def test_index_scope_replacement_exclusion_restore_and_mcp(self):
        aid = self.add(self.a,"shared","<!-- page: 2 -->\n# RF\n5.4 GHz ZCU216 load-pull.")
        self.add(self.a,"metadata")
        self.add(self.b,"private","private material")
        self.assertTrue(self.rag.index_once())
        self.assertTrue(self.rag.index_once())
        self.assertFalse(self.rag.index_once())
        hits = self.rag.search(self.a, "load-pull")
        self.assertEqual([(h["article_id"],h["page"]) for h in hits], [(aid,2)])
        self.assertEqual(self.rag.status(self.a)["indexed"],1)
        self.assertIn("[1]",self.rag.ask(self.a,"load-pull",model="chat")["answer"])
        self.add(self.b,"shared")
        self.assertTrue(self.rag.index_once())
        self.cat.exclude(self.a,aid)
        self.assertEqual(self.rag.search(self.a,"load-pull"),[])
        self.assertTrue(self.rag.index_once())
        self.assertEqual({p["article_id"] for p in self.rag.search(self.b,"RF")}, {aid,aid+2})
        self.cat.restore(self.a,aid)
        self.assertTrue(self.rag.index_once())
        with self.cat.connection(write=True) as db:
            db.execute("UPDATE articles SET body=? WHERE id=?", ("<!-- page: 4 -->\nNew edition",aid))
        self.assertEqual(self.rag.search(self.a,"RF"),[])
        self.assertTrue(self.rag.index_once())
        self.assertEqual(self.rag.read(self.a,aid,page=4)["passages"][0]["text"],"New edition")
        call = dict(jsonrpc="2.0", id=2, method="tools/call",params={"name":"search_library","arguments":{"library_id":self.a,"query":"RF"}})
        self.assertIn("edition", self.rag.mcp(call)["result"]["content"][0]["text"])
        self.cat.exclude(self.a,aid,global_scope=True)
        self.assertTrue(self.rag.index_once())
        self.assertFalse(any(p["payload"]["article_id"] == aid for ps in self.services.collections.values() for p in ps.values()))

    def test_schedule_fallback_model_choice_and_rebuild(self):
        p = self.settings["profiles"][0]
        secondary = dict(p, name="Backup", base_url="http://backup/v1", priority=20)
        self.settings["profiles"].append(secondary)
        self.rag.save_settings(self.settings)
        self.services.down.add("http://model/v1/embeddings")
        self.add(self.a,"one","source material")
        self.assertTrue(self.rag.index_once())
        self.assertEqual(self.rag.status(self.a)["chunks"],1)
        self.assertTrue(any(url=="http://backup/v1/embeddings" for url,_ in self.services.calls))
        self.settings["embedding_model"] = "new"
        self.settings["profiles"].append(dict(secondary,embedding_model="new",chat_model="chosen"))
        self.rag.save_settings(self.settings)
        self.assertEqual(self.rag.status(self.a)["pending"],1)
        self.assertEqual(self.rag.ask(self.a,"RF",model="chosen")["model"],"chosen")
        self.assertTrue(self.rag.index_once())
        self.assertTrue(self.rag.index_once())
        self.assertEqual(list(self.services.collections),[self.rag.collection("new")])
        with self.assertRaises(RagUnavailable):
            self.rag.ask(self.a,"RF",model="missing")
        self.assertEqual(self.rag.models(0), ["chat","embed"])
        self.assertEqual(self.rag.status(self.a)["pending"],0)
        monday = dict(p, days=[0], start="22:30",end="06:00",timezone="UTC")
        monday_noon = datetime(2026,11,30,12,tzinfo=timezone.utc)
        self.assertFalse(in_window(monday,monday_noon))
        self.assertTrue(in_window(monday,monday_noon.replace(hour=23)))
        self.assertTrue(in_window(monday,datetime(2026,12,1,5,59,tzinfo=timezone.utc)))
        self.assertFalse(in_window(monday,datetime(2026,12,1,6,tzinfo=timezone.utc)))
        self.settings["profiles"] = [dict(monday,days=[])]
        self.rag.save_settings(self.settings)
        self.add(self.a,"two","awaiting model")
        past=len(self.services.calls)
        self.assertFalse(self.rag.index_once())
        self.assertFalse(any(u.endswith("/embeddings") for u,_ in self.services.calls[past:]))

    def test_source_change_during_generation_and_invalid_citations(self):
        aid = self.add(self.a,"doc","Evidence")
        self.rag.index_once()
        self.services.answer = "Claim [2]"
        with self.assertRaisesRegex(RagUnavailable,"номерами"):
            self.rag.ask(self.a,"query")
        self.services.answer = "Claim [1]"
        self.services.on_chat = lambda: self.cat.exclude(self.a, aid)
        with self.assertRaisesRegex(RagUnavailable,"изменилась"):
            self.rag.ask(self.a,"query")
        with self.assertRaises(ValueError):
            clean_settings({"profiles":[{"base_url":"file:///path"}]})

    def test_native_lmstudio_mode_is_explicit_and_bounded(self):
        self.settings["profiles"][0].update(chat_mode="lmstudio_fast",max_tokens=56)
        self.rag.save_settings(self.settings)
        self.add(self.a,"paper","<!-- page: 2 -->\nTest evidence")
        self.assertTrue(self.rag.index_once())
        self.assertIn("[1]",self.rag.ask(self.a,"Frequency?")["answer"])
        url, request = self.services.calls[-1]
        self.assertEqual(url,"http://model/api/v1/chat")
        self.assertEqual(request["reasoning"],"off")
        self.assertFalse(request["store"])
        self.assertEqual(request["max_output_tokens"],56)
        self.assertIn("Test evidence",request["input"])

    def test_mcp_protocol_and_invalid_args(self):
        init = self.rag.mcp({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}})
        self.assertEqual(init["result"]["protocolVersion"],"2025-06-18")
        self.assertIsNone(self.rag.mcp({"jsonrpc":"2.0","method":"notifications/initialized"}))
        bad = self.rag.mcp({"jsonrpc":"2.0","method":"tools/call","id":3,"params":{"name":"read_article","arguments":{}}})
        self.assertEqual(bad["error"]["code"],-32602)

    def test_interrupted_index_resumes_and_lost_collection_restarts_at_zero(self):
        body = "\n".join(f"# Section {i}\nPassage {i}." for i in range(30))
        self.add(self.a, "long", body)
        for lose_collection in (False, True):
            with self.subTest(lose_collection=lose_collection):
                base = self.rag._embed
                batches = []
                def interrupt(settings, model, texts, **kwargs):
                    batches.append(texts)
                    if len(batches) == 2:
                        raise RagUnavailable("interrupted after first batch")
                    return base(settings, model, texts, **kwargs)
                with patch.object(self.rag,"_embed",side_effect=interrupt):
                    self.assertFalse(self.rag.index_once())
                with self.cat.connection(write=True) as db:
                    self.assertEqual(db.execute("SELECT done FROM rag_progress").fetchone()[0],12)
                    db.execute("UPDATE rag_index SET retry_at=0")
                if lose_collection:
                    self.services.collections.clear()
                restarted = Rag(self.cat, "http://qdrant")
                self.services.calls.clear()
                self.assertTrue(restarted.index_once())
                first_input = next(data["input"] for url,data in self.services.calls if url.endswith("/embeddings"))
                self.assertEqual(first_input[0], "Passage 0." if lose_collection else "Passage 12.")
                points = self.services.collections[restarted.collection("embed")]
                self.assertEqual(len(points),30)
                self.assertEqual({p["payload"]["chunk"] for p in points.values()}, set(range(30)))
                with self.cat.connection(write=True) as db:
                    db.execute("UPDATE articles SET body=body || char(10) || char(10)")

    def test_interactive_embedding_waits_for_busy_background_slot(self):
        start, release = threading.Event(), threading.Event()
        real = self.services
        def delayed(method,url,data=None,**kwargs):
            if url.endswith("/embeddings") and data["input"] == ["document"]:
                start.set()
                release.wait(3)
            return real(method,url,data,**kwargs)
        from concurrent.futures import ThreadPoolExecutor
        with patch("rag.http_json",side_effect=delayed), ThreadPoolExecutor(2) as pool:
            job = pool.submit(self.rag._embed,self.settings,"embed",["document"],background=True)
            self.assertTrue(start.wait(2))
            query = pool.submit(self.rag._embed,self.settings,"embed",["question"])
            release.set()
            self.assertEqual(job.result(4), query.result(4))

    def test_external_qdrant_unset_switch_isolation_and_credentials(self):
        self.add(self.a, "doc", "Text")
        self.services.collections["user_managed"] = {}
        with patch.dict(os.environ, {"QDRANT_URL":"http://trusted", "QDRANT_API_KEY":"test-secret"}):
            rag = Rag(self.cat)
            requests = []
            def observe(method,url,data=None,**kwargs):
                requests.append((url,kwargs.get("headers",{})))
                return self.services(method,url,data,**kwargs)
            with patch("rag.http_json", side_effect=observe):
                rag.save_settings(dict(self.settings,qdrant_url=""))
                self.assertFalse(rag.index_once())
                self.assertEqual(rag.status(self.a)["state"],"unconfigured")
                self.assertIn("Qdrant",rag.status(self.a)["last_error"])
                with self.assertRaises(RagUnavailable):
                    rag.search(self.a,"question")
                self.assertEqual(requests,[])
                rag.save_settings(dict(self.settings,qdrant_url="http://trusted"))
                with patch("rag.http_json",side_effect=RagUnavailable("offline")) as offline:
                    self.assertFalse(rag.index_once())
                    self.assertTrue(all("/collections/" in c.args[1] for c in offline.call_args_list))
                    self.assertEqual(rag.status(self.a)["pending"],1)
                self.assertTrue(rag.index_once())
                first = rag.collection("embed")
                self.assertTrue(any(url.startswith("http://trusted/") and h.get("api-key")=="test-secret"
                                    for url,h in requests))
                rag.save_settings(dict(self.settings,qdrant_url="http://other"))
                self.assertEqual(rag.status(self.a)["pending"],1)
                self.assertTrue(rag.index_once())
                self.assertFalse(rag.index_once())
                second = rag.collection("embed")
                self.assertNotEqual(first,second)
                self.assertFalse(any("api-key" in h for url,h in requests if url.startswith("http://other/")))
                self.assertEqual(set(self.services.collections),{"user_managed",first,second})
                with self.assertRaises(RagUnavailable):
                    rag._q("DELETE",first)
                rag.save_settings(dict(self.settings,qdrant_url="http://trusted"))
                self.assertEqual(rag.status(self.a)["indexed"],1)
                self.assertFalse(rag.index_once())
                rag._verified.clear()
                with patch("rag.http_json",side_effect=RagUnavailable("offline")):
                    self.assertFalse(rag.index_once())
                    self.assertEqual(rag.status(self.a)["state"],"waiting")
