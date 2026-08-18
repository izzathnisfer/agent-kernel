import pytest

from agentkernel.test.config import AKTestConfig
from agentkernel.test.test import Mode, Test


@pytest.fixture(autouse=True)
def isolated_test_config(monkeypatch):
    """Keep a test-config.yaml in the CWD from influencing the configured modes."""
    monkeypatch.setenv("AK_TEST_CONFIG_PATH_OVERRIDE", "/nonexistent/test-config.yaml")
    AKTestConfig._reset()
    yield
    AKTestConfig._reset()


@pytest.fixture
def register_mode(monkeypatch):
    """Back an unimplemented Mode with a stub, since fuzzy alone can't exercise a passing fallback."""
    calls = []

    def _register(mode: Mode, passes: bool):
        def stub(actual, expected, user_input, threshold):
            calls.append(mode)
            if not passes:
                raise AssertionError(f"stub {mode.value} comparison failed")

        funcname = f"_stub_{mode.value}_compare"
        monkeypatch.setattr(Test, funcname, staticmethod(stub), raising=False)
        monkeypatch.setitem(Test.MODE_FUNCNAME_MAP, mode, funcname)
        return calls

    return _register


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


def test_compare_defaults_to_configured_mode():
    """Without an explicit mode, compare() uses the configured mode (fuzzy by default)"""
    Test.compare("Hello World", ["Hello World!"], threshold=50)

    with pytest.raises(AssertionError, match="didn't pass the threshold score"):
        Test.compare("Hello World", ["Goodbye"], threshold=50)


def test_compare_without_fallback_mode_raises_primary_failure():
    """With no fallback configured, a failing primary mode raises its own error"""
    with pytest.raises(AssertionError, match="didn't pass the threshold score"):
        Test.compare("Hello World", ["Goodbye"], threshold=50, mode=Mode.FUZZY, fallback_mode=None)


def test_compare_fallback_mode_not_run_when_primary_passes(register_mode):
    """The fallback mode is skipped entirely when the primary mode passes"""
    calls = register_mode(Mode.SEMANTIC, passes=True)

    Test.compare("Hello World", ["Hello World!"], threshold=50, mode=Mode.FUZZY, fallback_mode=Mode.SEMANTIC)

    assert calls == []


def test_compare_fallback_mode_can_rescue_primary_failure(register_mode):
    """A fallback mode that passes makes the whole comparison pass"""
    calls = register_mode(Mode.SEMANTIC, passes=True)

    # fuzzy fails on these strings, the semantic fallback accepts them
    Test.compare("Hello World", ["Goodbye"], threshold=50, mode=Mode.FUZZY, fallback_mode=Mode.SEMANTIC)

    assert calls == [Mode.SEMANTIC]


def test_compare_reports_both_modes_when_fallback_also_fails(register_mode):
    """When both modes fail the error names the primary mode and the fallback"""
    calls = register_mode(Mode.SEMANTIC, passes=False)

    with pytest.raises(AssertionError, match="didn't pass 'fuzzy' comparison or the 'semantic' fallback"):
        Test.compare("Hello World", ["Goodbye"], threshold=50, mode=Mode.FUZZY, fallback_mode=Mode.SEMANTIC)

    assert calls == [Mode.SEMANTIC]


def test_compare_any_mode_can_be_the_primary_mode(register_mode):
    """The primary mode isn't restricted to fuzzy - any mode can lead, with any mode behind it"""
    calls = register_mode(Mode.SEMANTIC, passes=False)

    # semantic runs first and fails, fuzzy runs as the fallback and passes
    Test.compare("Hello World", ["Hello World!"], threshold=50, mode=Mode.SEMANTIC, fallback_mode=Mode.FUZZY)

    assert calls == [Mode.SEMANTIC]


def test_compare_modes_come_from_config(monkeypatch, register_mode):
    """mode / fallback_mode are read from AKTestConfig when not passed explicitly"""
    calls = register_mode(Mode.SEMANTIC, passes=True)
    monkeypatch.setenv("AK_TEST__MODE", "fuzzy")
    monkeypatch.setenv("AK_TEST__FALLBACK_MODE", "semantic")
    AKTestConfig._reset()

    # fuzzy passes on its own, so the configured fallback stays untouched
    Test.compare("Hello World", ["Hello World!"], threshold=50)
    assert calls == []

    # fuzzy fails, so the configured fallback runs and rescues the comparison
    Test.compare("Hello World", ["Goodbye"], threshold=50)
    assert calls == [Mode.SEMANTIC]


def test_compare_unimplemented_mode_does_not_trigger_fallback():
    """A mode with no implementation is a configuration error, not a comparison failure"""
    with pytest.raises(NotImplementedError, match="'semantic' is not implemented yet"):
        Test.compare("Hello World", ["Goodbye"], threshold=50, mode=Mode.SEMANTIC, fallback_mode=Mode.FUZZY)


def test_compare_invalid_mode():
    """Test that an invalid mode raises ValueError"""
    with pytest.raises(ValueError, match="Invalid mode for 'mode'"):
        Test.compare("Hello", ["Hello"], mode="invalid")


def test_compare_invalid_fallback_mode():
    """Test that an invalid fallback mode raises ValueError"""
    with pytest.raises(ValueError, match="Invalid mode for 'fallback_mode'"):
        Test.compare("Hello", ["Hello"], mode=Mode.FUZZY, fallback_mode="invalid")


def test_compare_accepts_mode_strings(register_mode):
    """Modes may be passed as their string values as well as Mode members"""
    register_mode(Mode.SEMANTIC, passes=True)

    Test.compare("Hello World", ["Hello World!"], threshold=50, mode="fuzzy")
    Test.compare("Hello World", ["Goodbye"], threshold=50, mode="fuzzy", fallback_mode="semantic")
