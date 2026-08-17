from typing import List, Optional, Type, Union

from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase
from pydantic import BaseModel

from ..frameworks.deepeval import DeepevalEvaluator
from .evaluator import AKEvaluator

_ModelType = Optional[Union[str, DeepEvalBaseLLM]]


class DeepevalAKEvaluator(AKEvaluator):
    """AKEvaluator implementation backed by the `deepeval` framework's `DeepevalEvaluator`."""

    def __init__(self, model: _ModelType = None):
        self._deepeval = DeepevalEvaluator(model=model)

    def exact(self, target: str, prediction: str) -> int:
        """Deterministic exact-match comparison via `deepeval`'s exact-match scorer."""
        return self._deepeval.exact_match_score(target=target, prediction=prediction)

    def fuzzy(self, target: str, prediction: str) -> int:
        """Lexical similarity via `deepeval`'s normalized (case/punctuation/whitespace) quasi exact-match scorer."""
        return self._deepeval.quasi_exact_match_score(target=target, prediction=prediction)

    def overlap(self, target: str, prediction: str, score_type: str = "rougeL") -> float:
        """N-gram statistical overlap via `deepeval`'s ROUGE scorer."""
        return self._deepeval.rouge_score(target=target, prediction=prediction, score_type=score_type)

    def semantic(self, references: Union[str, List[str]], predictions: Union[str, List[str]]) -> float:
        """Embedding-based semantic similarity via `deepeval`'s BERTScore scorer."""
        return self._deepeval.bert_score(references=references, predictions=predictions)

    def judge(self, test_case: LLMTestCase, criteria: Optional[str] = None, threshold: float = 0.5):
        """LLM-as-judge comparison via `deepeval`'s G-Eval."""
        return self._deepeval.g_eval(test_case=test_case, criteria=criteria, threshold=threshold)

    def safety(self, test_case: LLMTestCase, threshold: float = 0.5):
        """Unary guardrail-style check via `deepeval`'s hallucination metric."""
        return self._deepeval.hallucination(test_case=test_case, threshold=threshold)

    def structural(self, test_case: LLMTestCase, expected_schema: Type[BaseModel], threshold: float = 0.5):
        """Format/shape conformance via `deepeval`'s JSON-correctness metric."""
        return self._deepeval.json_correctness(test_case=test_case, expected_schema=expected_schema, threshold=threshold)

    def human(self):
        """Human-in-the-loop rating; not automatable by `deepeval`."""
        raise NotImplementedError("Human-in-the-loop evaluation is not automated.")
