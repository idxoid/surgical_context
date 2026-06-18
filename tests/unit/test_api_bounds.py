"""Request validation bounds for search limit and token_budget (Pydantic → HTTP 422)."""

import pytest
from pydantic import ValidationError

from context_engine.main import (
    SEARCH_LIMIT_MAX,
    SEARCH_LIMIT_MIN,
    TOKEN_BUDGET_MAX,
    TOKEN_BUDGET_MIN,
    AskRequest,
    SearchRequest,
    UnifiedSearchRequest,
)


@pytest.mark.parametrize("limit", [0, -1, SEARCH_LIMIT_MAX + 1])
def test_search_request_rejects_out_of_bounds_limit(limit):
    with pytest.raises(ValidationError):
        SearchRequest(query="test", limit=limit)


def test_search_request_accepts_bounds_edges():
    SearchRequest(query="test", limit=SEARCH_LIMIT_MIN)
    SearchRequest(query="test", limit=SEARCH_LIMIT_MAX)


@pytest.mark.parametrize("token_budget", [0, TOKEN_BUDGET_MIN - 1, TOKEN_BUDGET_MAX + 1])
def test_ask_request_rejects_out_of_bounds_token_budget(token_budget):
    with pytest.raises(ValidationError):
        AskRequest(question="q", token_budget=token_budget)


def test_ask_request_accepts_bounds_edges():
    AskRequest(question="q", token_budget=TOKEN_BUDGET_MIN)
    AskRequest(question="q", token_budget=TOKEN_BUDGET_MAX)


def test_unified_search_request_rejects_out_of_bounds_token_budget():
    with pytest.raises(ValidationError):
        UnifiedSearchRequest(query="q", limit=5, token_budget=TOKEN_BUDGET_MAX + 1)
