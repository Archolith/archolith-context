"""Tests for fact-level compression at assembly time."""

import pytest

from archolith_proxy.assembler.compress import (
    compress_fact,
    compress_facts_batch,
)


class TestCompressFact:
    def test_strips_hedging_prefix(self):
        result = compress_fact("It was found that the config file is missing a required key")
        assert result == "The config file is missing a required key"

    def test_strips_based_on_analysis(self):
        result = compress_fact("Based on the analysis, src/auth.py has 3 functions")
        assert result == "src/auth.py has 3 functions"

    def test_strips_assistant_found(self):
        result = compress_fact("The assistant found that tests are passing")
        assert result == "Tests are passing"

    def test_strips_it_appears(self):
        result = compress_fact("It appears that the database connection is timing out")
        assert result == "The database connection is timing out"

    def test_strips_upon_inspection(self):
        result = compress_fact("Upon inspection, the module exports 5 functions")
        assert result == "The module exports 5 functions"

    def test_strips_in_current_session(self):
        result = compress_fact("In the current session, 3 files were modified")
        assert result == "3 files were modified"

    def test_collapses_responsible_for(self):
        result = compress_fact("auth.py is responsible for handling authentication")
        assert result == "auth.py handles authentication"

    def test_collapses_has_been_modified_to(self):
        result = compress_fact("The config has been modified to use port 8080")
        assert result == "The config changed to use port 8080"

    def test_collapses_in_order_to(self):
        result = compress_fact("Changed the import in order to fix circular dependency")
        assert result == "Changed the import to fix circular dependency"

    def test_collapses_due_to_fact_that(self):
        result = compress_fact("Failed due to the fact that the key is expired")
        assert result == "Failed because the key is expired"

    def test_removes_trailing_period(self):
        result = compress_fact("Tests passed successfully.")
        assert result == "Tests passed successfully"

    def test_preserves_ellipsis(self):
        result = compress_fact("Loading modules..")
        assert result == "Loading modules.."

    def test_preserves_file_paths(self):
        result = compress_fact("src/auth/middleware.py exports handleAuth function")
        assert result == "src/auth/middleware.py exports handleAuth function"

    def test_preserves_numbers(self):
        result = compress_fact("It was found that there are 42 test files across 3 directories")
        assert result == "There are 42 test files across 3 directories"

    def test_preserves_error_types(self):
        result = compress_fact("TypeError: Cannot read property 'id' of undefined at line 15")
        assert result == "TypeError: Cannot read property 'id' of undefined at line 15"

    def test_strips_redundant_notably(self):
        result = compress_fact("Notably, the auth module has no tests")
        assert result == "The auth module has no tests"

    def test_strips_please_note(self):
        result = compress_fact("Please note that the API key is stored in .env")
        assert result == "The API key is stored in .env"

    def test_handles_empty_string(self):
        assert compress_fact("") == ""

    def test_handles_none_like_empty(self):
        assert compress_fact("   ") == "   "

    def test_preserves_path_starting_fact(self):
        result = compress_fact("src/main.py is the entry point")
        assert result == "src/main.py is the entry point"

    def test_does_not_overcapitalize_paths(self):
        result = compress_fact("It was found that config.yaml has invalid syntax")
        assert result == "config.yaml has invalid syntax"

    def test_combined_compression(self):
        verbose = "Based on the analysis, the function handleAuth in src/auth/middleware.py is responsible for handling JWT token validation."
        result = compress_fact(verbose)
        assert "handleAuth" in result
        assert "src/auth/middleware.py" in result
        assert "JWT token validation" in result
        assert "Based on" not in result
        assert len(result) < len(verbose)

    def test_truncation_with_max_tokens(self):
        long_fact = "A " * 500
        result = compress_fact(long_fact, max_tokens=20)
        assert len(result) <= 85  # 20*4 + "..."
        assert result.endswith("...")


class TestCompressFactsBatch:
    def test_batch_compression(self):
        facts = [
            {"content": "It was found that tests are passing", "fact_type": "state"},
            {"content": "Based on analysis, src/app.py exports main()", "fact_type": "observation"},
            {"content": "TypeError at line 42", "fact_type": "error"},
        ]
        compressed, ratio = compress_facts_batch(facts)
        assert len(compressed) == 3
        assert ratio > 1.0  # Some compression happened
        assert compressed[0]["content"] == "Tests are passing"
        assert compressed[1]["content"] == "src/app.py exports main()"
        assert compressed[2]["content"] == "TypeError at line 42"

    def test_preserves_original_content(self):
        facts = [{"content": "It was found that X is true", "fact_type": "state"}]
        compressed, _ = compress_facts_batch(facts)
        assert compressed[0]["_original_content"] == "It was found that X is true"
        assert compressed[0]["content"] == "X is true"

    def test_preserves_other_fields(self):
        facts = [{"content": "test", "fact_type": "error", "confidence": 0.9, "source_turn": 3}]
        compressed, _ = compress_facts_batch(facts)
        assert compressed[0]["fact_type"] == "error"
        assert compressed[0]["confidence"] == 0.9
        assert compressed[0]["source_turn"] == 3

    def test_empty_batch(self):
        compressed, ratio = compress_facts_batch([])
        assert compressed == []
        assert ratio == 1.0

    def test_ratio_reflects_savings(self):
        facts = [
            {"content": "Based on the analysis, it was determined that the module exports 5 functions"},
            {"content": "src/main.py"},
        ]
        _, ratio = compress_facts_batch(facts)
        assert ratio > 1.0
