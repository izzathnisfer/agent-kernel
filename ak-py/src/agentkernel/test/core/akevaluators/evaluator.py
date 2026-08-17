from abc import ABC, abstractmethod


class AKEvaluator(ABC):
    """Base interface for response-comparison strategies used by the test harness."""

    @abstractmethod
    def exact(self):
        """Deterministic exact-match comparison."""

    @abstractmethod
    def fuzzy(self):
        """Lexical similarity comparison (e.g. edit-distance)."""

    @abstractmethod
    def overlap(self):
        """N-gram statistical overlap comparison (e.g. ROUGE/BLEU)."""

    @abstractmethod
    def semantic(self):
        """Embedding-based semantic similarity comparison."""

    @abstractmethod
    def judge(self):
        """LLM-as-judge comparison (generative scoring against a rubric/criteria)."""

    @abstractmethod
    def safety(self):
        """Unary guardrail-style check on the output alone (e.g. bias, toxicity, hallucination)."""

    @abstractmethod
    def structural(self):
        """Format/shape conformance (e.g. schema/JSON validation, regex pattern match, expected tool calls)."""

    @abstractmethod
    def human(self):
        """Human-in-the-loop rating, for cases no automated method can resolve."""