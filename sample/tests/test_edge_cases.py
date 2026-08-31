"""Boundary and malformed-input tests shared across the sample components."""
import json

import pytest

from code_extraction import extract_code
from config import ProviderSpec, _env_var_from_url, resolve_provider
from models import MBPPTaskInput, SandboxConfig, SWEBenchTaskInput
from sandbox.executor import Sandbox


def test_extraction_handles_empty_and_whitespace_only_output() -> None:
    for value in ("", "   \n\t"):
        result = extract_code(value)
        assert result.code is None
        assert "[NoCodeBlock]" in result.note


def test_extraction_rejects_malformed_alternate_formats() -> None:
    assert extract_code("<tool_call>{bad}</tool_call>").code is None
    result = extract_code('Action: search_code\nAction Input: {"bad"}')
    assert result.code is not None
    assert 'value=\'{"bad"}\'' in result.code
    assert extract_code('<invoke name="x"><parameter name="a">').code is None


def test_extraction_preserves_json_null_boolean_and_numbers() -> None:
    result = extract_code(
        '<tool_call>{"name":"tool","arguments":{"a":null,"b":true,"c":1}}</tool_call>'
    )
    assert result.code == "result = tool(a=None, b=True, c=1)\nprint(result)"


def test_provider_url_normalization_and_environment_name() -> None:
    assert resolve_provider("https://api.groq.com/openai/v1/").name == "groq"
    assert _env_var_from_url("https://example.com:8443/api") == "EXAMPLE_COM_8443_API_KEY"


def test_provider_key_collection_stops_at_first_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_KEY", "one")
    monkeypatch.setenv("EDGE_KEY_3", "three")
    assert ProviderSpec("edge", "https://example.invalid", "EDGE_KEY").collect_api_keys() == ["one"]


def test_sandbox_empty_code_and_missing_name_are_explicit() -> None:
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    assert "[NoCodeBlock]" in sandbox.run("   ")
    assert "NameError" in sandbox.run("print(missing_name)")


def test_sandbox_default_getattr_is_honored() -> None:
    sandbox = Sandbox(
        SandboxConfig(authorized_imports=[], allowed_directories=[]),
        apply_process_memory_limit=False,
    )
    assert sandbox.run("print(getattr(object(), 'missing', None))").strip() == "None"


def test_models_reject_missing_required_fields() -> None:
    with pytest.raises(Exception):
        MBPPTaskInput.model_validate({})
    with pytest.raises(Exception):
        SWEBenchTaskInput.model_validate({})


def test_models_round_trip_unicode_and_empty_optional_fields() -> None:
    task = MBPPTaskInput.model_validate(
        {
            "task_id": 0,
            "task_definition": "文字列を処理する",
            "function_definition": "def solve(x):",
            "test_list": [],
        }
    )
    assert json.loads(task.model_dump_json())["task_id"] == 0
