"""Tests for the ForecastLedger MCP server.

Pure stdlib (unittest) so the suite runs under both pytest and
``python -m unittest`` with no extra dependencies. Every test uses the
committed fixture registry at tests/fixtures/registry.json — no external
service is contacted and no real registry is touched.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SERVER_DIR = REPO_ROOT / "server"
FIXTURE_REGISTRY = HERE / "fixtures" / "registry.json"

for _entry in (str(SERVER_DIR), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import mcp_server  # noqa: E402

FIXTURE = json.loads(FIXTURE_REGISTRY.read_text(encoding="utf-8"))

RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602

EXPECTED_TOOLS = ["get_registry_summary", "get_horizon_calls", "get_registry"]


def make_server(registry_path=None):
    if registry_path is None:
        registry_path = str(FIXTURE_REGISTRY)
    else:
        registry_path = str(registry_path)
    return mcp_server.ForecastLedgerServer(registry_path=registry_path)


def run_server(lines, env_overrides=None, timeout=30):
    """Feed newline-delimited lines to the server subprocess and collect
    every response line. Returns (response_dicts, raw_stdout, stderr, rc).

    By default points FORECASTLEDGER_REGISTRY_PATH at the committed fixture
    so tests exercise the tools instead of the deployment default path; a
    test can override it via env_overrides (e.g. the missing-file case)."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if isinstance(getattr(sys, "base_prefix", ""), str) and sys.prefix != sys.base_prefix or os.name == "nt":
        # Virtualenvs launched via sys.executable carry their own site dirs.
        for key in ("PYTHONHOME", "VIRTUAL_ENV"):
            env[key] = os.environ.get(key, "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("FORECASTLEDGER_REGISTRY_PATH", str(FIXTURE_REGISTRY))
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_DIR / "mcp_server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(REPO_ROOT),
    )
    payload = "".join(line + "\n" for line in lines).encode("utf-8")
    out, err = proc.communicate(payload, timeout=timeout)
    responses = []
    for raw_line in out.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line:
            responses.append(json.loads(line))
    return responses, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace"), proc.returncode


def rpc(id_, method, params=None):
    message = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


class DirectHandlerTests(unittest.TestCase):
    """Cover the in-process handlers using the fixture registry."""

    def setUp(self):
        self.server = make_server()

    def _ok_text(self, call_result):
        # tools/call handlers return the MCP CallToolResult shape directly:
        # {"content": [{"type": "text", "text": ...}], "isError": bool}
        self.assertFalse(call_result.get("isError"), call_result)
        self.assertIsInstance(call_result.get("content"), list)
        self.assertGreaterEqual(len(call_result["content"]), 1)
        self.assertEqual(call_result["content"][0].get("type"), "text")
        return call_result["content"][0]["text"]

    def test_tools_list_three_tools_clean_schemas(self):
        payload = self.server.handle_tools_list(None)
        tools = payload["tools"]
        self.assertEqual([t["name"] for t in tools], EXPECTED_TOOLS)
        for tool in tools:
            self.assertTrue(tool.get("description"))
            self.assertEqual(tool["inputSchema"]["type"], "object")
            json.dumps(tool)  # every schema must be JSON-serialisable
        schema = tools[1]["inputSchema"]
        self.assertEqual(schema["properties"]["horizon"]["enum"], list(mcp_server.HORIZONS))
        self.assertEqual(schema["properties"]["status"]["enum"], list(mcp_server.STATUSES))
        self.assertEqual(schema["required"], ["horizon"])

    def test_get_registry_summary_returns_fixture_headline(self):
        obj = self.server.handle_tools_call({"name": "get_registry_summary", "arguments": {}})
        text = self._ok_text(obj)
        doc = json.loads(text)
        self.assertEqual(doc["generated_ts"], FIXTURE["generated_ts"])
        self.assertEqual(doc["registry_start"], FIXTURE["registry_start"])
        self.assertEqual(doc["framing"], FIXTURE["meta"]["framing"])
        for horizon in mcp_server.HORIZONS:
            self.assertIn(horizon, doc["horizons"])
            expected = FIXTURE["horizons"][horizon]["stats"]
            got = doc["horizons"][horizon]
            self.assertEqual(got["n_graded"], expected["n_graded"])
            self.assertEqual(got["hit_rate"], expected["hit_rate"])
            self.assertEqual(got["brier"], expected["brier"])
            self.assertEqual(got["baseline_brier"], expected["baseline_brier"])
            self.assertEqual(got["armed"], expected["armed"])

    def test_get_horizon_calls_returns_fixture_calls(self):
        obj = self.server.handle_tools_call(
            {"name": "get_horizon_calls", "arguments": {"horizon": "24h"}}
        )
        text = self._ok_text(obj)
        doc = json.loads(text)
        fixture_calls = FIXTURE["horizons"]["24h"]["calls"]
        self.assertEqual(doc["count"], len(fixture_calls))
        self.assertEqual(doc["calls"], fixture_calls)
        for call in doc["calls"]:
            self.assertIn(call["status"], mcp_server.STATUSES)
            if call["status"] == "resolved":
                self.assertIn(call["verdict"], ("inside", "above", "below"))
                self.assertIsNotNone(call["realized_px"])

    def test_status_filter_open_and_resolved(self):
        for status, expect in (("open", 1), ("resolved", 2), (None, 3)):
            args = {"horizon": "24h"}
            if status is not None:
                args["status"] = status
            obj = self.server.handle_tools_call({"name": "get_horizon_calls", "arguments": args})
            doc = json.loads(self._ok_text(obj))
            self.assertEqual(doc["count"], expect)
            for call in doc["calls"]:
                if status is not None:
                    self.assertEqual(call["status"], status)

    def test_unknown_horizon_is_structured_error_not_crash(self):
        obj = self.server.handle_tools_call(
            {"name": "get_horizon_calls", "arguments": {"horizon": "2w"}}
        )
        self.assertTrue(obj["isError"])
        self.assertIn("horizon", obj["content"][0]["text"])
        json.dumps(obj)  # must still be a well-formed result payload

    def test_unknown_status_is_structured_error(self):
        obj = self.server.handle_tools_call(
            {"name": "get_horizon_calls", "arguments": {"horizon": "24h", "status": "expired"}}
        )
        self.assertTrue(obj["isError"])

    def test_unknown_tool_is_structured_error(self):
        obj = self.server.handle_tools_call({"name": "get_scorecard", "arguments": {}})
        self.assertTrue(obj["isError"])
        self.assertIn("get_scorecard", obj["content"][0]["text"])

    def test_registry_error_on_missing_file(self):
        missing = os.path.join(tempfile.gettempdir(), "forecastledger-missing-dir", "registry.json")
        server = mcp_server.ForecastLedgerServer(registry_path=missing)
        with self.assertRaises(mcp_server.RegistryError):
            server.load_registry()

    def test_registry_error_on_directory(self):
        server = make_server(registry_path=HERE)
        with self.assertRaises(mcp_server.RegistryError):
            server.load_registry()

    def test_registry_error_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "broken.json"
            bad.write_text("{ not json", encoding="utf-8")
            server = make_server(registry_path=bad)
            with self.assertRaises(mcp_server.RegistryError) as ctx:
                server.load_registry()
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_dispatch_unknown_method(self):
        resp = self.server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(resp["error"]["code"], RPC_METHOD_NOT_FOUND)
        self.assertEqual(resp["id"], 9)

    def test_dispatch_ping(self):
        resp = self.server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(resp["result"], {})

    def test_serve_line_malformed_yields_parse_error(self):
        resp = self.server.serve_line("{definitely not json")
        self.assertEqual(resp["error"]["code"], RPC_PARSE_ERROR)
        self.assertIsNone(resp["id"])

    def test_serve_line_blank_and_notification_silent(self):
        self.assertIsNone(self.server.serve_line(""))
        self.assertIsNone(self.server.serve_line("   "))
        notification = json.dumps({"jsonrpc": "2.0", "method": "ping"})
        self.assertIsNone(self.server.serve_line(notification))


class StdioProtocolTests(unittest.TestCase):
    """End-to-end tests over the real stdio loop (subprocess)."""

    def test_initialize_tools_list_and_call_roundtrip(self):
        responses, _, err, rc = run_server(
            [
                rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}),
                rpc(2, "tools/list"),
                rpc(3, "tools/call", {"name": "get_registry_summary", "arguments": {}}),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses), 3, f"stderr={err!r}")
        init = responses[0]["result"]
        self.assertEqual(init["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(init["serverInfo"]["name"], "forecast-ledger")
        names = [t["name"] for t in responses[1]["result"]["tools"]]
        self.assertEqual(names, EXPECTED_TOOLS)
        call = responses[2]
        self.assertEqual(call["id"], 3)
        self.assertNotIn("error", call)
        self.assertFalse(call["result"]["isError"])
        doc = json.loads(call["result"]["content"][0]["text"])
        self.assertEqual(doc["generated_ts"], FIXTURE["generated_ts"])
        self.assertEqual(doc["horizons"]["24h"]["n_graded"], FIXTURE["horizons"]["24h"]["stats"]["n_graded"])

    def test_thirty_malformed_lines_then_valid_request(self):
        # Strictly unparseable as JSON: parse-error (-32700) per line, and
        # the server must keep answering afterwards.
        malformed = ["{not json", "]]]", "\"bare string", "{", "}", "x-", "@!", "{,", "[,", "null null", "{'a':1}"]
        garbage = (malformed * 3)[:30]
        responses, _, err, rc = run_server(
            garbage + [rpc(7, "tools/call", {"name": "get_registry_summary", "arguments": {}})]
        )
        self.assertEqual(rc, 0, f"server exited non-zero; stderr={err!r}")
        self.assertEqual(len(responses), 31)
        for i, resp in enumerate(responses[:30]):
            self.assertEqual(resp["error"]["code"], RPC_PARSE_ERROR, f"line {i}")
            self.assertIsNone(resp["id"])
        self.assertEqual(responses[30]["id"], 7)
        self.assertTrue(responses[30]["result"]["content"])
        doc = json.loads(responses[30]["result"]["content"][0]["text"])
        self.assertEqual(doc["generated_ts"], FIXTURE["generated_ts"])

    def test_missing_registry_yields_structured_error_result(self):
        bogus = os.path.join(tempfile.gettempdir(), "forecastledger-nonexistent-registry.json")
        env = {"FORECASTLEDGER_REGISTRY_PATH": bogus, "PYTHONIOENCODING": "utf-8"}
        responses, _, err, rc = run_server(
            [rpc(5, "tools/call", {"name": "get_registry_summary", "arguments": {}})],
            env_overrides=env,
        )
        self.assertEqual(rc, 0, f"stderr={err!r}")
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 5)
        self.assertNotIn("error", responses[0])
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("registry", responses[0]["result"]["content"][0]["text"])

    def test_unknown_horizon_over_stdio(self):
        responses, _, err, rc = run_server(
            [rpc(11, "tools/call", {"name": "get_horizon_calls", "arguments": {"horizon": "6h"}})]
        )
        self.assertEqual(rc, 0, f"stderr={err!r}")
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("6h", result["content"][0]["text"])

    def test_unknown_method_error_code(self):
        responses, _, err, rc = run_server(
            [rpc(12, "sampling/createMessage", {})]
        )
        self.assertEqual(rc, 0, f"stderr={err!r}")
        self.assertEqual(responses[0]["error"]["code"], RPC_METHOD_NOT_FOUND)

    def test_mixed_traffic_loop_survives(self):
        lines = [
            "garbage-not-json",
            rpc(20, "ping"),
            "more: [broken",
            rpc(21, "tools/call", {"name": "get_horizon_calls", "arguments": {"horizon": "3d"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            rpc(22, "tools/list"),
        ]
        responses, _, err, rc = run_server(lines)
        self.assertEqual(rc, 0, f"stderr={err!r}")
        by_id = {resp["id"]: resp for resp in responses if resp.get("id") is not None}
        # Two parse errors (id null), three answered requests (20/21/22);
        # the notification is intentionally unanswered per JSON-RPC.
        self.assertEqual(sorted(by_id), [20, 21, 22])
        self.assertEqual(responses[0]["error"]["code"], RPC_PARSE_ERROR)
        self.assertEqual(responses[2]["error"]["code"], RPC_PARSE_ERROR)
        self.assertEqual(by_id[20]["result"], {})
        self.assertFalse(by_id[21]["result"]["isError"])
        calls3d = json.loads(by_id[21]["result"]["content"][0]["text"])
        self.assertEqual(calls3d["count"], len(FIXTURE["horizons"]["3d"]["calls"]))
        self.assertEqual([t["name"] for t in by_id[22]["result"]["tools"]], EXPECTED_TOOLS)

    def test_valid_json_non_object_yields_invalid_request(self):
        # Valid JSON, wrong shape: '[]' parses but is not a request object.
        responses, _, err, rc = run_server(["[]", rpc(30, "ping")])
        self.assertEqual(rc, 0, f"stderr={err!r}")
        self.assertEqual(responses[0]["error"]["code"], RPC_INVALID_REQUEST)
        self.assertEqual(responses[1]["id"], 30)

    def test_unknown_method_error_code(self):
        responses, _, err, rc = run_server([rpc(12, "sampling/createMessage", {})])
        self.assertEqual(rc, 0, f"stderr={err!r}")
        self.assertEqual(responses[0]["error"]["code"], RPC_METHOD_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()