"""Independent regression sample for issue #7598; production selector imports only."""

from types import SimpleNamespace

from deerflow.models.selection import resolve_model_control


def catalog(data=False):
    names = ["gpt-default", "data-chat" if data else "claude-sonnet"]
    return [SimpleNamespace(name=n, model=n, display_name=n, aliases=[]) for n in names]


def test_original_bug():
    result = resolve_model_control("Use a regression model to analyze sales", catalog())
    assert result["status"] == "none"
    assert result["task_offset"] == 0


def test_preservation():
    result = resolve_model_control("Use Python to analyze sales", catalog())
    assert result["status"] == "none"
    assert result["task_offset"] == 0


def test_explicit_valid():
    result = resolve_model_control("Use gpt-default for this turn: summarize", catalog())
    assert result["status"] == "resolved"
    assert result["scope"] == "turn"


def test_explicit_unknown():
    result = resolve_model_control("Use the Gemini model for this turn: summarize", catalog())
    assert result["status"] == "unavailable"
    assert result["scope"] == "turn"


def test_ordinary_similar():
    result = resolve_model_control("Use a causal model to analyze sales", catalog())
    assert result["status"] == "none"
    assert result["task_offset"] == 0


def test_adjacent_boundary():
    result = resolve_model_control("Use a data model to design a database", catalog(data=True))
    assert result["status"] == "none"
    assert result["task_offset"] == 0


def test_metamorphic():
    result = resolve_model_control("USE   a regression model   TO analyze sales", catalog())
    assert result["status"] == "none"
    assert result["task_offset"] == 0
