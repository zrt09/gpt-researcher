"""T06: adaptive planning prompts."""

from gpt_researcher.prompts import PromptFamily


def test_plan_prompt_contains_question_and_limit():
    prompt = PromptFamily.generate_plan_prompt("What limits RAG systems?", max_tasks=4)
    assert "What limits RAG systems?" in prompt
    assert "at most 4" in prompt
    assert '"query"' in prompt and '"goal"' in prompt


def test_plan_prompt_has_no_unsupported_operators_instruction():
    prompt = PromptFamily.generate_plan_prompt("q", max_tasks=3)
    assert "site:" in prompt  # explicitly forbidden in the instructions
    assert "natural language" in prompt


def test_gap_prompt_includes_deficit_and_optional_claim():
    prompt = PromptFamily.generate_gap_query_prompt("coverage missing for T2")
    assert "coverage missing for T2" in prompt
    assert "Claim under investigation" not in prompt

    with_claim = PromptFamily.generate_gap_query_prompt(
        "weak evidence", claim="RAG hallucin ates more."
    )
    assert "weak evidence" in with_claim
    assert "RAG hallucin ates more." in with_claim


def test_existing_prompts_are_untouched():
    # Regression guard: the new methods must not change existing ones.
    prompt = PromptFamily.generate_search_queries_prompt(
        question="q", parent_query="", report_type="research_report", max_iterations=3
    )
    assert "Write 3 search queries" in prompt
