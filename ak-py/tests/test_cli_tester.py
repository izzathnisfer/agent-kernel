import pytest

from agentkernel.test.test import Mode, Test


def test_compare_uses_threshold():
    # Should pass with a reasonable fuzzy threshold when expected is similar
    Test.compare("Hello World", ["Hello World!"], threshold=60, mode=Mode.FUZZY)

    # Now raise a threshold too high to fail
    with pytest.raises(AssertionError):
        Test.compare("Hello World", ["Hi there"], threshold=95, mode=Mode.FUZZY)


def test_compare_fuzzy_mode():
    """Test fuzzy mode only uses fuzzy matching"""
    # Should pass with fuzzy matching
    Test.compare("Hello World", ["Hello World!"], threshold=50, mode=Mode.FUZZY)

    # Should fail with fuzzy matching when threshold is too high
    with pytest.raises(AssertionError, match="didn't pass the threshold score"):
        Test.compare("Hello World", ["Goodbye"], threshold=50, mode=Mode.FUZZY)


def test_compare_judge_mode():
    """Judge mode is a placeholder pending the AKEvaluator migration (Ragas support removed)."""
    with pytest.raises(NotImplementedError):
        Test.compare(
            actual="Paris is the capital of France",
            expected=["The capital of France is Paris"],
            user_input="What is the capital of France?",
            mode=Mode.JUDGE,
            threshold=50,
        )


def test_compare_fallback_mode():
    """Test fallback mode tries fuzzy first, then judge (currently a placeholder)"""
    # Should pass with fuzzy matching (no judge call needed)
    Test.compare("Hello World", ["Hello World!"], threshold=50)

    # When fuzzy fails, fallback attempts judge, which is not yet implemented
    with pytest.raises(NotImplementedError):
        Test.compare(
            "Hello",
            ["Goodbye completely"],
            user_input="What is the capital of France?",
            threshold=95,
            mode=Mode.FALLBACK,
        )


def test_compare_invalid_mode():
    """Test that invalid mode raises ValueError"""
    with pytest.raises(ValueError, match="Invalid mode"):
        Test.compare("Hello", ["Hello"], mode="invalid")


def test_compare_with_different_modes():
    """Test each mode independently, all via the same static Test.compare() entry point"""
    # Fuzzy mode
    Test.compare("Hello World", ["Hello World!"], threshold=50, mode=Mode.FUZZY)

    # Judge mode (placeholder pending the AKEvaluator migration)
    with pytest.raises(NotImplementedError):
        Test.compare(
            actual="Paris is the capital of France",
            expected=["The capital of France is Paris"],
            user_input="What is the capital of France?",
            mode=Mode.JUDGE,
            threshold=50,
        )

    # Fallback mode (default)
    Test.compare("Hello World", ["Hello World!"], threshold=50)
