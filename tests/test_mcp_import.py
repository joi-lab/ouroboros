"""One-way MCP client-file import preview: translation, matching, problems and the route.

Values are synthetic. The preview is pure: it never connects, starts, saves or
changes the draft list it is given. The draft list is matching metadata only
(``MCPImportDraftServer``): identities, transports and header names.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import mcp_client
from ouroboros.mcp_import import preview_import

BEARER = "Bearer synthetic-import-0001"
CUSTOM = "synthetic-import-key-0002"
ENV_SECRET = "synthetic-import-env-0003"


def preview(document, draft=()):
    draft = list(draft)
    before = copy.deepcopy(draft)
    result = preview_import(document if isinstance(document, str) else json.dumps(document), draft)
    assert draft == before
    return result


def entries(document, draft=()):
    result = preview(document, draft)
    assert result["ok"], result
    return {entry["source_name"]: entry for entry in result["entries"]}


def displayed(entry):
    return json.dumps({key: value for key, value in entry.items() if key != "patch"})


def test_new_http_and_stdio_servers_are_disabled_adds_with_literal_values():
    found = entries({"mcpServers": {
        "Docs": {"type": "http", "url": "https://docs.example/mcp",
                 "headers": {"Authorization": BEARER, "X-Api-Key": CUSTOM}},
        "files": {"command": "npx", "args": ["-y", "server", "--token", ENV_SECRET],
                  "env": {"API_TOKEN": ENV_SECRET}, "cwd": "/srv/files"},
    }})
    docs, files = found["Docs"], found["files"]
    assert docs["action"] == files["action"] == "add"
    assert docs["patch"] == {"id": "docs", "name": "Docs", "enabled": False, "transport": "streamable_http",
                             "url": "https://docs.example/mcp",
                             "headers": {"Authorization": BEARER, "X-Api-Key": CUSTOM}}
    assert "auth_token" not in docs["patch"] and "auth_header" not in docs["patch"]
    assert docs["header_names"] == ["Authorization", "X-Api-Key"]
    assert files["patch"]["args"] == ["-y", "server", "--token", ENV_SECRET] and files["arg_count"] == 4
    assert files["env_names"] == ["API_TOKEN"] and "ordinary visible settings" in files["warnings"][0]
    for entry in (docs, files):
        for secret in (BEARER, CUSTOM, ENV_SECRET):
            assert secret not in displayed(entry)


@pytest.mark.parametrize("declared, transport", [
    ({"type": "http"}, "streamable_http"), ({"type": "streamable_http"}, "streamable_http"),
    ({"type": "streamable-http"}, "streamable_http"),
    ({"transport": "sse"}, "sse"), ({}, "streamable_http"),
    ({"type": "sse", "transport": "sse"}, "sse"),
])
def test_http_transport_names_map_explicitly(declared, transport):
    entry = entries({"mcpServers": {"a": {**declared, "url": "https://a.example/mcp"}}})["a"]
    assert entry["transport"] == entry["patch"]["transport"] == transport


@pytest.mark.parametrize("raw, problem", [
    ({"type": "websocket", "url": "https://a.example"}, "type must be one of"),
    ({"type": "http", "transport": "sse", "url": "https://a.example"}, "name different transports"),
    ({"url": "https://a.example", "command": "npx"}, "both url and command"),
    ({"type": "stdio", "url": "https://a.example"}, "url cannot be used with transport stdio"),
    ({"type": "sse"}, "needs url"),
    ({"headers": {"X-A": "b"}}, "needs url"),
    ({"url": "ftp://a.example"}, "http:// or https://"),
    ({"url": "https://a.example", "headers": [["X-A", CUSTOM]]}, "headers must be an object"),
    ({"url": "https://a.example", "headers": {"X-A": 7}}, "must be a string"),
    ({"url": "https://a.example", "headers": {"X-A": CUSTOM, "x-a": CUSTOM}}, "differ only by case"),
    ({"url": "https://a.example", "headers": None}, "null is not a value for: headers"),
    ({"command": "npx", "args": "-y server"}, "args must be a list of strings"),
    ({"command": "npx", "env": {"TOKEN": 5}}, "env values must be strings"),
    ({"command": ["npx"]}, "command must be a string"),
    ("not an object", "must be an object"),
])
def test_malformed_entries_are_problems_not_partial_imports(raw, problem):
    entry = entries({"mcpServers": {"a": raw}})["a"]
    assert not entry["action"] and entry["patch"] == {}
    assert any(problem in text for text in entry["problems"]), entry["problems"]
    assert CUSTOM not in displayed(entry)


@pytest.mark.parametrize("text, error", [
    ('{"mcpServers": {"a": {"url": "https://a.example"}, "a": {}}}', "duplicate JSON object key"),
    ('{"mcpServers": {"a": {"url": "https://a.example", "headers": {"X": "1", "X": "2"}}}}', "duplicate JSON object key"),
    ('{"mcpServers": {"a": ', "not valid JSON"),
    ('[]', "top-level mcpServers or servers object"),
    ('{"server": {}}', "top-level mcpServers or servers object"),
    ('{"mcpServers": []}', "top-level mcpServers or servers object"),
    ('{"servers": []}', "top-level mcpServers or servers object"),
    ('{"mcpServers": {}, "servers": {}}', "both mcpServers and servers"),
])
def test_unreadable_documents_are_refused_whole(text, error):
    result = preview(text)
    assert result["ok"] is False and error in result["error"] and result["entries"] == []


def test_unsupported_keys_are_named_without_values_and_other_top_level_keys_are_ignored():
    result = preview({"mcpServers": {"a": {"url": "https://a.example/mcp", "disabled": False,
                                           "headersHelper": "print-" + CUSTOM, "oauth": {"secret": CUSTOM}}},
                      "globalShortcut": "Ctrl+Space"})
    entry = result["entries"][0]
    assert entry["action"] == "add" and entry["unsupported_keys"] == ["disabled", "headersHelper", "oauth"]
    assert set(entry["patch"]) == {"id", "name", "enabled", "transport", "url"}
    assert result["ignored_keys"] == ["globalShortcut"] and CUSTOM not in json.dumps(result)


def test_url_credentials_are_never_displayed(monkeypatch):
    url = f"https://user:{CUSTOM}@docs.example/mcp"
    refused = entries({"mcpServers": {"a": {"url": url}}})["a"]
    assert "username/password" in refused["problems"][0] and CUSTOM not in json.dumps(refused)
    monkeypatch.setattr(mcp_client, "mode_has_unrestricted_agency", lambda _mode: True)
    accepted = entries({"mcpServers": {"a": {"url": url}}})["a"]
    assert "url" not in accepted and CUSTOM not in displayed(accepted)
    assert accepted["patch"]["url"] == url


DRAFT = [
    {"id": "docs", "name": "Docs (renamed)", "transport": "streamable_http",
     "header_names": ["Authorization", "X-Tenant"], "legacy_header": "Authorization"},
    {"id": "files", "transport": "stdio", "header_names": [], "legacy_header": None},
]


def test_update_touches_only_named_fields_and_keeps_every_header_when_headers_is_absent():
    found = entries({"mcpServers": {
        "Docs": {"url": "https://docs-v2.example/mcp"},
        "files": {"command": "uvx", "cwd": ""},
    }}, DRAFT)
    assert found["Docs"]["action"] == "update" and found["Docs"]["index"] == 0
    assert found["Docs"]["patch"] == {"transport": "streamable_http", "url": "https://docs-v2.example/mcp"}
    assert found["Docs"]["kept_header_names"] == ["Authorization", "X-Tenant"]
    assert "header_changes" not in found["Docs"] and found["Docs"]["legacy_header"] == "Authorization"
    assert found["files"]["patch"] == {"transport": "stdio", "command": "uvx", "cwd": ""}
    assert "kept_header_names" not in found["files"] and not found["Docs"]["warnings"]


def test_present_headers_replace_the_whole_list_and_clear_the_legacy_pair_in_the_same_patch():
    replaced = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp",
                                                "headers": {"X-Api-Key": CUSTOM, "x-tenant": "t2"}}}}, DRAFT)["docs"]
    assert replaced["patch"] == {"transport": "streamable_http", "url": "https://docs.example/mcp",
                                 "headers": {"X-Api-Key": CUSTOM, "x-tenant": "t2"},
                                 "auth_header": "Authorization", "auth_token": ""}
    assert replaced["header_changes"] == {"added": ["X-Api-Key"], "replaced": ["X-Tenant"],
                                          "removed": ["Authorization"]}
    assert replaced["legacy_header"] == "Authorization" and not replaced["warnings"]
    cleared = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp", "headers": {}}}}, DRAFT)["docs"]
    assert cleared["patch"]["headers"] == {} and cleared["patch"]["auth_token"] == ""
    assert cleared["header_changes"] == {"added": [], "replaced": [], "removed": ["Authorization", "X-Tenant"]}


def test_imported_authorization_replaces_the_saved_single_token_instead_of_colliding():
    entry = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp",
                                             "headers": {"authorization": BEARER}}}}, DRAFT)["docs"]
    assert entry["action"] == "update" and entry["patch"]["headers"] == {"authorization": BEARER}
    assert entry["patch"]["auth_token"] == "" and entry["header_changes"]["replaced"] == ["Authorization"]
    assert not entry["warnings"] and BEARER not in displayed(entry)
    plain = [dict(DRAFT[0], header_names=["X-Tenant"], legacy_header=None), DRAFT[1]]
    entry = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp",
                                             "headers": {"Authorization": BEARER}}}}, plain)["docs"]
    assert entry["patch"]["auth_token"] == "" and "legacy_header" not in entry


@pytest.mark.parametrize("headers", [{}, {"X-Api-Key": CUSTOM}])
def test_present_headers_clear_the_legacy_pair_even_when_the_draft_no_longer_shows_it(headers):
    """The draft row was removed unsaved: its server object still holds the saved token, so the patch clears it."""
    removed = [dict(DRAFT[0], header_names=["X-Tenant"], legacy_header=None), DRAFT[1]]
    entry = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp", "headers": headers}}}, removed)["docs"]
    assert entry["patch"]["headers"] == headers
    assert (entry["patch"]["auth_header"], entry["patch"]["auth_token"]) == ("Authorization", "")
    assert entry["header_changes"]["removed"] == ["X-Tenant"] and "legacy_header" not in entry
    kept = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp"}}}, removed)["docs"]
    assert "auth_token" not in kept["patch"] and kept["kept_header_names"] == ["X-Tenant"]


def test_an_unreadable_saved_map_is_named_when_a_file_replaces_it():
    unreadable = [dict(DRAFT[0], header_names=[], legacy_header=None, unreadable_headers=True)]
    entry = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp",
                                             "headers": {"X-Api-Key": CUSTOM}}}}, unreadable)["docs"]
    assert entry["unreadable_headers"] is True and entry["header_changes"]["added"] == ["X-Api-Key"]


def test_a_transport_change_is_refused_with_remove_and_add_guidance():
    entry = entries({"mcpServers": {"docs": {"type": "sse", "url": "https://docs.example/sse"}}}, DRAFT)["docs"]
    assert not entry["action"] and "remove that server first" in entry["problems"][0]


def test_canonical_identity_collisions_are_ambiguous_on_both_sides():
    found = entries({"mcpServers": {"GitHub": {"url": "https://a.example"}, "github": {"url": "https://b.example"},
                                    "!!!": {"url": "https://c.example"}}})
    for name in ("GitHub", "github"):
        assert "several imported names" in found[name]["problems"][0] and not found[name]["action"]
    assert "no usable server ID" in found["!!!"]["problems"][0]
    twice = [{"id": "docs"}, {"name": "Docs"}]
    entry = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp"}}}, twice)["docs"]
    assert "several draft servers" in entry["problems"][0] and not entry["action"]


def test_the_route_is_no_store_and_refuses_a_malformed_request():
    from ouroboros.gateway import mcp

    app = Starlette(routes=[Route("/preview", mcp.api_mcp_import_preview, methods=["POST"])])
    with TestClient(app) as client:
        document = {"mcpServers": {"docs": {"url": "https://docs.example/mcp", "headers": {"X-Api-Key": CUSTOM}}}}
        response = client.post("/preview", json={"text": json.dumps(document), "servers": [*DRAFT, None]})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["entries"][0]["action"] == "update"
        for body, reason in (({"text": 1, "servers": []}, "text must be a JSON string"),
                             ({"text": "{}", "servers": {}}, "servers must be a JSON array"),
                             (["text"], "payload must be a JSON object"),
                             ({"text": "{}", "servers": [{"id": "a", "header_names": "X-A"}]},
                              "servers[0] matches no allowed shape"),
                             ({"servers": []}, "text is required")):
            refused = client.post("/preview", json=body)
            assert refused.status_code == 400 and refused.headers["cache-control"] == "no-store"
            assert reason in refused.json()["error"], (body, refused.json())


@pytest.mark.parametrize("text", [
    '{"mcpServers":{"a":{"headers":{"synthetic-private-key":"one","synthetic-private-key":"two"}}}}',
    '{"mcpServers":{"a":{"url":"synthetic-private-key://example.test"}}}',
    '{"mcpServers":{"a":{"url":"https://example.test:synthetic-private-key"}}}',
    '{"mcpServers":{"a":{"url":"https://[synthetic-private-key]/"}}}',
    '{"mcpServers": synthetic-private-key}',
])
def test_parse_and_validation_errors_do_not_echo_input_values(text, caplog):
    result = preview(text)
    assert "synthetic-private-key" not in json.dumps(result)
    assert "synthetic-private-key" not in caplog.text


def test_preview_neither_contacts_services_nor_reads_settings_or_saves(monkeypatch):
    from ouroboros import config
    from ouroboros.gateway import mcp

    def unexpected(*args, **kwargs):
        raise AssertionError("Preview crossed an I/O boundary")

    monkeypatch.setattr(mcp, "_ensure_configured", unexpected)
    monkeypatch.setattr(mcp, "get_manager", unexpected)
    monkeypatch.setattr(mcp, "load_settings", unexpected)
    monkeypatch.setattr(config, "load_settings", unexpected)
    monkeypatch.setattr(mcp_client, "_transport_factory", unexpected)
    app = Starlette(routes=[Route("/preview", mcp.api_mcp_import_preview, methods=["POST"])])
    with TestClient(app) as client:
        response = client.post("/preview", json={"text": json.dumps({"mcpServers": {
            "a": {"url": "https://example.test/mcp", "headers": {"X-Key": CUSTOM}},
            "b": {"command": "npx", "args": ["--sample"], "env": {"PORT": "8080"}},
        }}), "servers": [None, {"id": "unrelated", "headers": ["legacy"]}]})
    assert response.status_code == 200 and response.json()["ok"]
    assert all(row["action"] == "add" for row in response.json()["entries"])


def test_literals_in_url_and_command_stay_only_in_the_patch():
    found = entries({"mcpServers": {
        "a": {"url": "https://example.test/" + CUSTOM + "?token=" + CUSTOM},
        "b": {"command": "synthetic-command-" + CUSTOM},
    }})
    assert all(CUSTOM not in displayed(row) for row in found.values())
    assert CUSTOM in found["a"]["patch"]["url"] and CUSTOM in found["b"]["patch"]["command"]


def test_vs_code_servers_wrapper_reads_the_same_entries():
    result = preview({"servers": {"Docs": {"type": "http", "url": "https://docs.example/mcp",
                                           "headers": {"X-Api-Key": CUSTOM}}},
                      "inputs": [{"id": "key", "type": "promptString", "password": True}]})
    assert result["ok"] and result["wrapper"] == "servers" and result["ignored_keys"] == ["inputs"]
    assert result["entries"][0]["action"] == "add" and result["entries"][0]["patch"]["headers"] == {"X-Api-Key": CUSTOM}


def test_substitution_text_is_kept_literally_with_a_warning_that_names_fields_not_values():
    entry = entries({"mcpServers": {
        "a": {"url": "https://example.test/${env:PATH_PART}", "headers": {"Authorization": "Bearer ${API_KEY}",
                                                                          "X-Plain": CUSTOM}},
        "b": {"command": "npx", "args": ["--token", "${input:token}"], "env": {"TOKEN": "${TOKEN:-x}", "PORT": "1"}},
    }})
    a, b = entry["a"], entry["b"]
    assert a["patch"]["headers"]["Authorization"] == "Bearer ${API_KEY}" and a["action"] == "add"
    assert a["warnings"][0].startswith("url, header Authorization: ${...} is kept as written")
    assert b["warnings"][0].startswith("args, environment TOKEN: ${...} is kept as written")
    for row in (a, b):
        assert "API_KEY" not in displayed(row) and "input:token" not in displayed(row) and CUSTOM not in displayed(row)


def test_the_settings_keep_marker_is_not_imported_as_a_header_value():
    entry = entries({"mcpServers": {"docs": {"url": "https://docs.example/mcp",
                                             "headers": {"X-Api-Key": "***set***", "X-Other": CUSTOM}}}}, DRAFT)["docs"]
    assert not entry["action"] and entry["patch"] == {}
    assert "header X-Api-Key has the value ***set***" in entry["problems"][0] and CUSTOM not in displayed(entry)


def test_matching_reads_only_identity_transport_and_header_names():
    # A full draft object with values gives the same answer as its metadata.
    full = [{"id": "docs", "url": "https://secret.example/?k=" + CUSTOM, "auth_header": "Authorization",
             "auth_token": "Bearer t...", "headers": {"X-Tenant": "***set***"}, "transport": "streamable_http",
             "header_names": ["Authorization", "X-Tenant"], "legacy_header": "Authorization"}]
    document = {"mcpServers": {"docs": {"url": "https://docs.example/mcp", "headers": {"X-Api-Key": "k"}}}}
    assert preview(document, full) == preview(document, DRAFT[:1])


def test_preview_contract_twin_and_the_producer_agree():
    import typing

    from ouroboros.gateway import mcp_import_contracts as contracts
    from tests.test_gateway_parity import _js_typedef_fields

    twin = (pathlib.Path(__file__).resolve().parents[1] / "web" / "modules" / "mcp_import_types.js").read_text(
        encoding="utf-8")
    for name in contracts.__all__:
        cls = getattr(contracts, name)
        hints = typing.get_type_hints(cls, include_extras=True)
        assert _js_typedef_fields(twin, name) == set(hints), name
        optional = {field for field, hint in hints.items() if typing.get_origin(hint) is contracts.NotRequired}
        if not cls.__total__:
            optional = set(hints)
        declared = {line.split("}")[-1].split()[0] for line in twin.split(f"@typedef {{Object}} {name}\n")[1]
                    .split("\n */")[0].splitlines() if "@property {" in line and "=}" in line}
        assert declared == optional, name
    result = preview({"mcpServers": {"docs": {"url": "https://docs.example/mcp", "headers": {"X-A": "1"}},
                                     "files": {"command": "uvx"}, "bad": 7}}, DRAFT)
    entry_hints = typing.get_type_hints(contracts.MCPImportPreviewEntry, include_extras=True)
    entry_fields = set(entry_hints)
    # Postponed annotations hide NotRequired from __optional_keys__; the resolved hints keep it.
    required = {field for field, hint in entry_hints.items() if typing.get_origin(hint) is not contracts.NotRequired}
    for entry in result["entries"]:
        assert required <= set(entry) <= entry_fields, sorted(set(entry) ^ entry_fields)
    assert set(result) <= set(typing.get_type_hints(contracts.MCPImportPreviewResponse))
    api_types = (pathlib.Path(__file__).resolve().parents[1] / "web" / "modules" / "api_types.js").read_text(
        encoding="utf-8")
    assert "mcp_import_types.js" in api_types.splitlines()[0]
    from ouroboros.gateway.endpoint_index import HTTP_ENDPOINTS
    assert "POST /api/mcp/import/preview" in HTTP_ENDPOINTS
    # The gateway's derived ingress schema reads the twin like any contract TypedDict.
    from ouroboros.gateway.schema import json_schema_for, validate_ingress
    schema = json_schema_for(contracts.MCPImportPreviewRequest)
    assert schema["required"] == ["servers", "text"]
    item = schema["properties"]["servers"]["items"]
    assert {"type": "null"} in item["anyOf"] and any(option.get("properties", {}).get("legacy_header")
                                                    for option in item["anyOf"])
    assert validate_ingress({"text": "{}", "servers": [None, {"id": "a", "header_names": ["X"],
                                                              "legacy_header": None}]},
                            contracts.MCPImportPreviewRequest) == []
