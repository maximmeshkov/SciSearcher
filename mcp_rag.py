"""Stateless MCP tools over the application's authenticated Streamable HTTP POST endpoint."""
import json

from rag import RagUnavailable


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


ID = {"type": "integer", "minimum": 1}
TOOLS = [
    {"name": "list_libraries", "description": "List available research libraries with their IDs and full-text counts.",
     "inputSchema": schema({}, [])},
    {"name": "search_library", "description": "Semantic search in the full texts of one library. Returns quoted passages, source titles, DOI and available PDF page links.",
     "inputSchema": schema({"library_id": ID, "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                            "limit": {"type":"integer", "minimum":1, "maximum":12}}, ["library_id","query"])},
    {"name": "read_article", "description": "Read article passages from the selected library. Optionally restrict to a 1-based PDF page. Continue with next_offset.",
     "inputSchema": schema({"library_id": ID, "article_id": ID, "page": ID,
                            "offset": {"type":"integer","minimum":0}}, ["library_id","article_id"])}
]
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def dispatch(rag, request):
    rid = request.get("id") if isinstance(request, dict) else None
    def response(key, data):
        return {"jsonrpc": "2.0", "id": rid, key: data}
    def error(code, message):
        return response("error", {"code": code, "message": message})
    if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or
            not isinstance(request.get("method"), str) or ("id" in request and type(rid) not in (int,str))):
        return error(-32600, "Invalid request")
    if "id" not in request:
        return None
    method, params = request["method"], request.get("params", {})
    if not isinstance(params, dict):
        return error(-32602, "Invalid params")
    if method == "initialize":
        version = params.get("protocolVersion")
        return response("result", {"protocolVersion":version if version in VERSIONS else VERSIONS[0],
                        "capabilities":{"tools":{}}, "serverInfo":{"name":"SciSearcher","version":"1.0"}})
    if method == "ping":
        return response("result", {})
    if method == "tools/list":
        return response("result", {"tools": TOOLS})
    if method != "tools/call":
        return error(-32601, "Method not found")
    name, args = params.get("name"), params.get("arguments", {})
    tool = next((tool for tool in TOOLS if tool["name"] == name), None)
    if not tool or not isinstance(args, dict):
        return error(-32602, "Unknown tool or invalid arguments")
    spec = tool["inputSchema"]
    if not set(spec["required"]) <= args.keys() <= spec["properties"].keys():
        return error(-32602, "Missing or unexpected tool arguments")
    try:
        if name == "list_libraries":
            result = [{k: item[k] for k in ("id", "name", "full_text_count")} for item in rag.catalog.libraries()]
        elif name == "search_library":
            result = rag.search(**args)
        else:
            result = rag.read(**args)
    except (ValueError, LookupError, RagUnavailable) as e:
        return response("result", {"isError":True, "content":[{"type":"text", "text":str(e)}]})
    return response("result", {"content":[{"type":"text","text":json.dumps(result, ensure_ascii=False)}]})
