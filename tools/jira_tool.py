#!/usr/bin/env python3
"""Jira tools -- separate Jira Cloud read and write access.

This module exposes two tools:
- ``jira_read`` for searches and read-only Jira REST calls
- ``jira_write`` for mutating Jira REST calls

Credentials may be supplied directly or by env-var name indirection.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from tools.registry import registry

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_SEARCH_FIELDS = "summary"
DEFAULT_SEARCH_LIMIT = 100
DEFAULT_BASE_URL = "https://example.atlassian.net"


READ_SCHEMA = {
    "name": "jira_read",
    "description": (
        "Read Jira Cloud data. Use jql for searches, or path for arbitrary read-only Jira endpoints. "
        "Credentials can be passed directly as values or as env var names to read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "baseUrlEnv": {
                "type": "string",
                "description": "Environment variable name containing the Jira Cloud base URL"
            },
            "emailEnv": {
                "type": "string",
                "description": "Environment variable name containing the Jira account email"
            },
            "apiTokenEnv": {
                "type": "string",
                "description": "Environment variable name containing the Jira API token"
            },
            "baseUrl": {
                "type": "string",
                "description": "Direct Jira Cloud base URL value, e.g. https://example.atlassian.net"
            },
            "email": {
                "type": "string",
                "description": "Direct Jira account email address used for Basic Auth"
            },
            "apiToken": {
                "type": "string",
                "description": "Direct Jira API token used for Basic Auth"
            },
            "account": {
                "type": "string",
                "description": "Optional label for the credential set being used"
            },
            "method": {
                "type": "string",
                "enum": ["GET"],
                "description": "Read-only HTTP method"
            },
            "path": {
                "type": "string",
                "description": (
                    "Jira REST path, e.g. /rest/api/3/myself or /rest/api/3/issue. "
                    "If omitted and jql is provided, /rest/api/3/search/jql is used."
                )
            },
            "jql": {
                "type": "string",
                "description": (
                    "Convenience search query. When set, the tool uses /rest/api/3/search/jql "
                    "and automatically adds fields/maxResults unless explicitly overridden."
                )
            },
            "fields": {
                "type": ["string", "array"],
                "items": {
                    "type": "string"
                },
                "description": "Fields for search requests. Defaults to summary when jql is used."
            },
            "maxResults": {
                "type": "integer",
                "description": "Maximum number of results when using jql search."
            },
            "query": {
                "type": "string",
                "description": "Raw query string appended to the path. Useful for advanced GET requests."
            }
        },
        "required": []
    }
}


WRITE_SCHEMA = {
    "name": "jira_write",
    "description": (
        "Write Jira Cloud data. Use path for Jira REST endpoints."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "baseUrlEnv": {
                "type": "string",
                "description": "Environment variable name containing the Jira Cloud base URL"
            },
            "emailEnv": {
                "type": "string",
                "description": "Environment variable name containing the Jira account email"
            },
            "apiTokenEnv": {
                "type": "string",
                "description": "Environment variable name containing the Jira API token"
            },
            "baseUrl": {
                "type": "string",
                "description": "Direct Jira Cloud base URL value, e.g. https://example.atlassian.net"
            },
            "email": {
                "type": "string",
                "description": "Direct Jira account email address used for Basic Auth"
            },
            "apiToken": {
                "type": "string",
                "description": "Direct Jira API token used for Basic Auth"
            },
            "account": {
                "type": "string",
                "description": "Optional label for the credential set being used"
            },
            "method": {
                "type": "string",
                "enum": ["POST", "PUT", "PATCH", "DELETE"],
                "description": "HTTP method. Defaults to POST."
            },
            "path": {
                "type": "string",
                "description": "Jira REST path, e.g. /rest/api/3/issue or /rest/api/3/issue/KEY/comment/123"
            },
            "query": {
                "type": "string",
                "description": "Raw query string appended to the path. Useful for advanced requests."
            },
            "body": {
                "type": ["object", "string"],
                "description": "JSON body for write requests. Strings are parsed as JSON when possible."
            },
        },
        "required": ["path"]
    }
}


def check_jira_requirements() -> bool:
    """Return True when the Jira tools can be registered."""
    return True


def _build_auth(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _coerce_body(value: Any) -> Optional[Any]:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _normalize_fields(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join(str(item) for item in value if str(item).strip())
    return str(value)


def _resolve_credential(args: dict, value_key: str, env_key: str) -> str:
    env_name = str(args.get(env_key) or "").strip()
    if env_name:
        return str(os.getenv(env_name, "")).strip()
    return str(args.get(value_key) or "").strip()


def _resolve_credentials(args: dict) -> tuple[str, str, str, str]:
    account = str(args.get("account") or "").strip() or "default"
    base_url = _resolve_credential(args, "baseUrl", "baseUrlEnv")
    email = _resolve_credential(args, "email", "emailEnv")
    token = _resolve_credential(args, "apiToken", "apiTokenEnv")
    return base_url, email, token, account


def _request_json(
    base_url: str,
    email: str,
    token: str,
    method: str,
    path: str,
    *,
    query: Optional[str] = None,
    body: Optional[Any] = None,
) -> tuple[int, str, str]:
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{query.lstrip('?')}"

    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Basic {_build_auth(email, token)}",
        "Accept": "application/json",
        "User-Agent": "Hermes-Jira-Tool/1.0",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, url, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Jira API request failed: {e}") from None


def _extract_tickets(payload: Any) -> list[dict[str, str]]:
    tickets: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return tickets
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return tickets
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = str(issue.get("key") or "").strip()
        summary = str(issue.get("fields", {}).get("summary") or "").strip()
        if key:
            tickets.append({"key": key, "summary": summary})
    return tickets


def _parse_response(text: str) -> Any:
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return text


def _resolve_search_request(
    method: str,
    path: Optional[str],
    jql: Optional[str],
    fields: Any,
    max_results: Any,
    query: Optional[str],
) -> tuple[str, str, Optional[str]]:
    if jql:
        request_path = path or "/rest/api/3/search/jql"
        if request_path.rstrip("/") == "/rest/api/3/search/jql":
            params = {"jql": jql}
            fields_value = _normalize_fields(fields)
            params["fields"] = fields_value or DEFAULT_SEARCH_FIELDS
            if max_results is not None and str(max_results) != "":
                params["maxResults"] = str(max_results)
            else:
                params["maxResults"] = str(DEFAULT_SEARCH_LIMIT)
            return method, request_path, urllib.parse.urlencode(params)
    request_path = path or "/rest/api/3/myself"
    return method, request_path, query


def _resolve_write_request(
    method: str,
    path: Optional[str],
    query: Optional[str],
    body: Any,
) -> tuple[str, str, Optional[str], Optional[Any]]:
    request_path = path or "/rest/api/3/issue"
    return method, request_path, query, body




def jira_read(args, **kwargs):
    """Hermes tool entry point for Jira reads and searches."""
    base_url, email, token, account_label = _resolve_credentials(args)

    if not base_url or not email or not token:
        return json.dumps({
            "error": (
                f"Jira credentials are missing for account '{account_label}'. "
                "Pass baseUrl/email/apiToken directly or pass baseUrlEnv/emailEnv/apiTokenEnv "
                "with env var names to read."
            )
        })

    method = str(args.get("method", "GET") or "GET").upper().strip()
    path = str(args.get("path") or "").strip() or None
    jql = str(args.get("jql") or "").strip() or None
    query = str(args.get("query") or "").strip() or None
    body = _coerce_body(args.get("body"))
    fields = args.get("fields")
    max_results = args.get("maxResults")

    if method != "GET":
        return json.dumps({"error": f"jira_read only supports GET, not {method}."})

    method, path, query = _resolve_search_request(
        method=method,
        path=path,
        jql=jql,
        fields=fields,
        max_results=max_results,
        query=query,
    )

    if body is not None:
        return json.dumps({"error": "jira_read does not accept a request body."})

    try:
        status, url, text = _request_json(
            base_url,
            email,
            token,
            method,
            path,
            query=query,
            body=None,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})

    parsed = _parse_response(text)
    result: dict[str, Any] = {"status": status, "method": method, "url": url}
    if isinstance(parsed, dict):
        result["data"] = parsed
        tickets = _extract_tickets(parsed)
        if tickets:
            result["tickets"] = tickets
    else:
        result["data"] = parsed
    return json.dumps(result, ensure_ascii=False)


def jira_write(args, **kwargs):
    """Hermes tool entry point for Jira writes."""
    base_url, email, token, account_label = _resolve_credentials(args)

    if not base_url or not email or not token:
        return json.dumps({
            "error": (
                f"Jira credentials are missing for account '{account_label}'. "
                "Pass baseUrl/email/apiToken directly or pass baseUrlEnv/emailEnv/apiTokenEnv "
                "with env var names to read."
            )
        })

    method = str(args.get("method", "POST") or "POST").upper().strip()
    path = str(args.get("path") or "").strip() or None
    query = str(args.get("query") or "").strip() or None
    body = _coerce_body(args.get("body"))

    if method not in WRITE_METHODS:
        return json.dumps({"error": f"jira_write only supports write methods, not {method}."})
    if not path:
        return json.dumps({"error": "jira_write requires a Jira REST path."})

    method, path, query, body = _resolve_write_request(method, path, query, body)

    try:
        status, url, text = _request_json(
            base_url,
            email,
            token,
            method,
            path,
            query=query,
            body=body,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})

    parsed = _parse_response(text)
    result: dict[str, Any] = {
        "status": status,
        "method": method,
        "url": url,
    }
    result["data"] = parsed
    if isinstance(parsed, dict):
        tickets = _extract_tickets(parsed)
        if tickets:
            result["tickets"] = tickets
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="jira_read",
    toolset="jira",
    schema=READ_SCHEMA,
    handler=jira_read,
    check_fn=check_jira_requirements,
    requires_env=[],
    description="Jira Cloud API tool for searches and reads",
    emoji="📋",
)

registry.register(
    name="jira_write",
    toolset="jira",
    schema=WRITE_SCHEMA,
    handler=jira_write,
    check_fn=check_jira_requirements,
    requires_env=[],
    description="Jira Cloud API tool for writes",
    emoji="✍️",
)

