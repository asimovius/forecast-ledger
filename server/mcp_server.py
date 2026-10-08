#!/usr/bin/env python3
"""ForecastLedger MCP server.

Serves the graded BTC market-forecast registry over the Model Context
Protocol (MCP) using stdio JSON-RPC 2.0, newline-delimited JSON (one JSON
object per line).

Protocol surface (v0.1):
    initialize   -> handshake (protocolVersion, capabilities, serverInfo)
    ping         -> {} liveness
    tools/list   -> the registry-reading tools
    tools/call   -> get_registry_summary | get_horizon_calls | get_registry

Design constraints:
    * stdlib-only; zero third-party runtime dependencies.
    * Read-only: the server reads one document (registry.json) and exposes
      derived artifacts only. It never writes, never opens sockets, and
      requires no credentials.
    * Failure stance: a missing, unreadable, or malformed registry yields a
      structured error result from tools/call. The stdio loop itself never
      crashes: malformed input lines produce JSON-RPC parse errors
      (-32700) and the loop keeps reading; unknown methods get -32601.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Iterator, List, Optional

SERVER_NAME = "forecast-ledger"
SERVER_VERSION = "0.1.0"

# MCP protocol revision this server speaks (pinned in server.json).
PROTOCOL_VERSION = "2025-06-18"

# Registry source. Environment override; default is the producer output path.
ENV_REGISTRY_PATH = "FORECASTLEDGER_REGISTRY_PATH"
DEFAULT_REGISTRY_PATH = "/root/business/registry/registry.json"

HORIZONS = ("24h", "3d", "7d", "4w")
STATUSES = ("resolved", "open")

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_registry_summary",
        "description": (
            "Headline calibration statistics for the graded BTC forecast "
            "registry: generated_ts, registry_start, and per-horizon "
            "n_graded / hit_rate / brier / baseline_brier / armed. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_horizon_calls",
        "description": (
            "List the graded calls for one horizon (24h, 3d, 7d, or 4w), "
            "each with id, issue timestamp, direction, probability band, "
            "status, verdict, realized price, and resolve timestamp. "
            "Optional status filter: resolved or open. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "horizon": {
                    "type": "string",
                    "enum": list(HORIZONS),
                    "description": "Forecast horizon to list calls for.",
                },
                "status": {
                    "type": "string",
                    "enum": list(STATUSES),
                    "description": (
                        "Optional filter: return only calls with this "
                        "status. Omitted returns all calls."
                    ),
                },
            },
            "required": ["horizon"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_registry",
        "description": (
            "Return the full registry document (data contract v1) as JSON: "
            "all horizons, their calls and stats, plus meta (attribution, "
            "framing). Derived artifacts only. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


class RegistryError(Exception):
    """Raised when the registry document cannot be read or is unusable."""


def _result(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _error_result(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _rpc_result(id_: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _fmt(value: Any) -> str:
    """Compact numeric formatting: 0.5 -> '0.5', None -> 'n/a'."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{float(value):.4g}"
    return str(value)


class ForecastLedgerServer:
    """Stateless registry reader speaking MCP over ndjson JSON-RPC 2.0."""

    def __init__(self, registry_path: Optional[str] = None) -> None:
        if registry_path is None:
            registry_path = os.environ.get(ENV_REGISTRY_PATH)
        self.registry_path = registry_path or DEFAULT_REGISTRY_PATH

    # -- registry access --------------------------------------------------
    def load_registry(self) -> Dict[str, Any]:
        path = self.registry_path
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            raise RegistryError(
                f"registry not found at path '{path}': set {ENV_REGISTRY_PATH} "
                "to the registry.json produced by the grader engine"
            )
        except IsADirectoryError:
            raise RegistryError(
                f"registry path '{path}' is a directory, expected registry.json"
            )
        except PermissionError:
            raise RegistryError(f"registry at path '{path}' is not readable")
        except json.JSONDecodeError as exc:
            raise RegistryError(
                f"registry at path '{path}' is not valid JSON: "
                f"line {exc.lineno} column {exc.colno}"
            )
        except OSError as exc:
            raise RegistryError(
                f"cannot read registry at path '{path}': {exc.strerror or exc}"
            )
        if not isinstance(doc, dict):
            raise RegistryError("registry document must be a JSON object")
        if not isinstance(doc.get("horizons"), dict):
            raise RegistryError(
                "registry document is missing the 'horizons' object "
                "(data contract v1)"
            )
        return doc

    # -- tool handlers ------------------------------------------------------
    def tool_get_registry_summary(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        reg = self.load_registry()
        horizons = reg.get("horizons") or {}
        out_horizons: Dict[str, Any] = {}
        for horizon in HORIZONS:
            entry = horizons.get(horizon)
            if not isinstance(entry, dict):
                continue
            stats = entry.get("stats") if isinstance(entry.get("stats"), dict) else {}
            out_horizons[horizon] = {
                "n_graded": stats.get("n_graded"),
                "hit_rate": stats.get("hit_rate"),
                "brier": stats.get("brier"),
                "baseline_brier": stats.get("baseline_brier", 0.25),
                "armed": bool(stats.get("armed", False)),
            }
        payload: Dict[str, Any] = {
            "generated_ts": reg.get("generated_ts"),
            "registry_start": reg.get("registry_start"),
            "horizons": out_horizons,
        }
        meta = reg.get("meta") if isinstance(reg.get("meta"), dict) else {}
        if meta.get("framing"):
            payload["framing"] = meta["framing"]
        return _result(json.dumps(payload, indent=2))

    def tool_get_horizon_calls(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        horizon = arguments.get("horizon")
        if not isinstance(horizon, str) or horizon not in HORIZONS:
            return _error_result(
                f"unknown horizon '{horizon}'; valid values: "
                + ", ".join(HORIZONS)
            )
        status = arguments.get("status")
        if status is not None and status not in STATUSES:
            return _error_result(
                f"unknown status '{status}'; valid values: " + ", ".join(STATUSES)
            )
        reg = self.load_registry()
        horizons = reg.get("horizons") or {}
        entry = horizons.get(horizon)
        if not isinstance(entry, dict):
            raise RegistryError(
                f"registry has no '{horizon}' horizon entry (data contract v1)"
            )
        calls = entry.get("calls")
        if not isinstance(calls, list):
            raise RegistryError(
                f"registry entry for '{horizon}' is missing the 'calls' list "
                "(data contract v1)"
            )
        if status is None:
            selected = list(calls)
        else:
            selected = [c for c in calls if isinstance(c, dict) and c.get("status") == status]
        payload = {
            "horizon": horizon,
            "status_filter": status,
            "count": len(selected),
            "calls": selected,
        }
        return _result(json.dumps(payload, indent=2))

    def tool_get_registry(self, _arguments: Dict[str, Any]) -> Dict[str, Any]:
        reg = self.load_registry()
        return _result(json.dumps(reg, indent=2))

    # -- MCP method handlers ------------------------------------------------
    def handle_initialize(self, params: Any) -> Dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def handle_tools_list(self, _params: Any) -> Dict[str, Any]:
        return {"tools": TOOLS}

    def handle_tools_call(self, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict):
            return _error_result("invalid params: 'params' must be an object")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise LookupError("missing tool name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error_result("invalid params: 'arguments' must be an object")
        if name == "get_registry_summary":
            return self.tool_get_registry_summary(arguments)
        if name == "get_horizon_calls":
            return self.tool_get_horizon_calls(arguments)
        if name == "get_registry":
            return self.tool_get_registry(arguments)
        known = ", ".join(t["name"] for t in TOOLS)
        return _error_result(f"unknown tool '{name}'; available tools: {known}")

    # -- dispatch -----------------------------------------------------------
    def dispatch(self, message: Any) -> Optional[Dict[str, Any]]:
        """Route one parsed JSON-RPC message. Returns the response object or
        None for notifications (no reply is ever sent for a notification)."""
        if not isinstance(message, dict):
            return _rpc_error(
                None,
                INVALID_REQUEST,
                "invalid request: one JSON object per line expected",
            )
        if "method" not in message:
            return _rpc_error(
                message.get("id"),
                INVALID_REQUEST,
                "invalid request: 'method' is missing",
            )
        method = message["method"]
        id_ = message.get("id")
        if "id" not in message:
            # Notification: JSON-RPC never replies to notifications.
            return None
        if method == "initialize":
            params = message.get("params")
            if params is not None and not isinstance(params, dict):
                return _rpc_error(id_, INVALID_PARAMS, "invalid params: 'params' must be an object")
            return _rpc_result(id_, self.handle_initialize(params))
        if method == "ping":
            return _rpc_result(id_, {})
        if method == "tools/list":
            return _rpc_result(id_, self.handle_tools_list(message.get("params")))
        if method == "tools/call":
            try:
                return _rpc_result(id_, self.handle_tools_call(message.get("params")))
            except LookupError as exc:
                return _rpc_error(id_, INVALID_PARAMS, f"invalid params: {exc}")
            except RegistryError as exc:
                return _rpc_result(id_, _error_result(str(exc)))
        return _rpc_error(id_, METHOD_NOT_FOUND, f"method not found: {method}")

    # -- line handling --------------------------------------------------------
    def serve_line(self, raw: Any) -> Optional[Dict[str, Any]]:
        """Turn one raw input line into a response dict (or None to stay
        silent). Malformed lines yield a parse error and never crash."""
        try:
            line = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        except (UnicodeDecodeError, AttributeError):
            line = raw if isinstance(raw, str) else str(raw)
        line = line.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            return _rpc_error(
                None,
                PARSE_ERROR,
                f"parse error: line {exc.lineno} column {exc.colno} ({exc.msg})",
            )
        try:
            return self.dispatch(message)
        except RegistryError as exc:
            id_ = message.get("id") if isinstance(message, dict) else None
            return _rpc_result(id_, _error_result(str(exc)))
        except Exception as exc:  # noqa: BLE001 - loop must survive anything
            id_ = message.get("id") if isinstance(message, dict) else None
            return _rpc_error(id_, INTERNAL_ERROR, f"internal error: {type(exc).__name__}")


def main() -> int:
    server = ForecastLedgerServer()
    try:
        for raw in sys.stdin:
            response = server.serve_line(raw)
            if response is None:
                continue
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())