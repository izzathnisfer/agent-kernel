from typing import List, Optional, Type, Union

from deepeval.metrics import (
    AnswerRelevancyMetric,
    BaseMetric,
    BiasMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    ContextualRelevancyMetric,
    ExactMatchMetric,
    FaithfulnessMetric,
    GEval,
    HallucinationMetric,
    JsonCorrectnessMetric,
    PatternMatchMetric,
    PromptAlignmentMetric,
    SummarizationMetric,
    ToolCorrectnessMetric,
    ToxicityMetric,
)
from deepeval.models import DeepEvalBaseLLM
from deepeval.scorer import Scorer
from deepeval.test_case import LLMTestCase, SingleTurnParams
from pydantic import BaseModel

_ModelType = Optional[Union[str, DeepEvalBaseLLM]]


class DeepevalEvaluator:
    """Standalone wrapper around the `deepeval` library's evaluation techniques.

    Provides one method per deepeval evaluation technique: G-Eval (custom LLM-judge
    rubrics), the built-in RAG/safety/agentic/format metrics, and the non-LLM statistical
    scorers from `deepeval.scorer.Scorer`.

    Metric-based methods (everything except the `*_score` scorer methods) accept a
    pre-built `LLMTestCase` and return the corresponding deepeval metric instance after
    calling `measure()` on it, so callers can inspect `.score`, `.reason` and
    `.is_successful()`. The `*_score` methods instead operate directly on plain
    strings and return a raw score, mirroring `Scorer`'s own signatures.
    """

    def __init__(self, model: _ModelType = None):
        """
        :param model: LLM judge to use for every LLM-based metric (a model name string, e.g.
            "gpt-4o-mini", or a `DeepEvalBaseLLM` instance). Defaults to deepeval's own default model.
        """
        self.model = model
        self._scorer = Scorer()

    @staticmethod
    def _measure(metric: BaseMetric, test_case: LLMTestCase) -> BaseMetric:
        metric.measure(test_case)
        return metric

    # LLM-as-judge

    def g_eval(
        self,
        test_case: LLMTestCase,
        criteria: Optional[str] = None,
        evaluation_steps: Optional[List[str]] = None,
        evaluation_params: Optional[List[SingleTurnParams]] = None,
        threshold: float = 0.5,
        name: str = "G-Eval",
    ) -> GEval:
        """
        Custom LLM-as-judge scoring using deepeval's G-Eval: a plain-English criteria
        (or explicit, reproducible evaluation steps) scored by an LLM via chain-of-thought.

        :param criteria: Plain-English description of what "correct"/"good" means. deepeval
            derives evaluation_steps from this automatically if evaluation_steps isn't given.
        :param evaluation_steps: Explicit step-by-step judging instructions, for
            deterministic/reproducible scoring instead of criteria auto-expansion.
        :param evaluation_params: Which LLMTestCase fields the judge is allowed to see
            (e.g. `[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT]`).
        :param threshold: Minimum score in [0.0, 1.0] required to pass.
        :param name: Name of the criteria being judged, used in reporting.
        :return: The measured GEval metric (`.score`, `.reason`, `.is_successful()`).
        """
        metric = GEval(
            name=name,
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            evaluation_params=evaluation_params,
            threshold=threshold,
            model=self.model,
        )
        return self._measure(metric, test_case)

    # RAG metrics

    def answer_relevancy(self, test_case: LLMTestCase, threshold: float = 0.5) -> AnswerRelevancyMetric:
        """Whether `actual_output` is relevant to `input`. Requires input, actual_output."""
        return self._measure(AnswerRelevancyMetric(threshold=threshold, model=self.model), test_case)

    def faithfulness(self, test_case: LLMTestCase, threshold: float = 0.5) -> FaithfulnessMetric:
        """Whether `actual_output` is factually grounded in `retrieval_context` (no hallucinated claims)."""
        return self._measure(FaithfulnessMetric(threshold=threshold, model=self.model), test_case)

    def contextual_precision(self, test_case: LLMTestCase, threshold: float = 0.5) -> ContextualPrecisionMetric:
        """Whether relevant nodes in `retrieval_context` are ranked above irrelevant ones."""
        return self._measure(ContextualPrecisionMetric(threshold=threshold, model=self.model), test_case)

    def contextual_recall(self, test_case: LLMTestCase, threshold: float = 0.5) -> ContextualRecallMetric:
        """Whether `retrieval_context` contains everything needed to produce `expected_output`."""
        return self._measure(ContextualRecallMetric(threshold=threshold, model=self.model), test_case)

    def contextual_relevancy(self, test_case: LLMTestCase, threshold: float = 0.5) -> ContextualRelevancyMetric:
        """Whether `retrieval_context` overall is relevant to `input`."""
        return self._measure(ContextualRelevancyMetric(threshold=threshold, model=self.model), test_case)

    # Safety metrics

    def hallucination(self, test_case: LLMTestCase, threshold: float = 0.5) -> HallucinationMetric:
        """Whether `actual_output` contradicts the known-correct facts in `context`."""
        return self._measure(HallucinationMetric(threshold=threshold, model=self.model), test_case)

    def bias(self, test_case: LLMTestCase, threshold: float = 0.5) -> BiasMetric:
        """Whether `actual_output` contains gender/racial/political/other bias."""
        return self._measure(BiasMetric(threshold=threshold, model=self.model), test_case)

    def toxicity(self, test_case: LLMTestCase, threshold: float = 0.5) -> ToxicityMetric:
        """Whether `actual_output` contains toxic content."""
        return self._measure(ToxicityMetric(threshold=threshold, model=self.model), test_case)

    # Summarization

    def summarization(
        self,
        test_case: LLMTestCase,
        threshold: float = 0.5,
        assessment_questions: Optional[List[str]] = None,
    ) -> SummarizationMetric:
        """
        Whether `actual_output` is a good summary of `input`: min(alignment, coverage), where
        alignment penalizes hallucinated detail and coverage penalizes missing key information.

        :param assessment_questions: Optional closed-ended yes/no questions used to score
            coverage. Auto-generated from `input` when omitted.
        """
        return self._measure(
            SummarizationMetric(threshold=threshold, model=self.model, assessment_questions=assessment_questions),
            test_case,
        )

    # Agentic / tool metrics

    def tool_correctness(self, test_case: LLMTestCase, threshold: float = 0.5) -> ToolCorrectnessMetric:
        """Whether `tools_called` matches `expected_tools` (both lists of `ToolCall`)."""
        return self._measure(ToolCorrectnessMetric(threshold=threshold), test_case)

    # Format / structural metrics

    def json_correctness(
        self,
        test_case: LLMTestCase,
        expected_schema: Type[BaseModel],
        threshold: float = 0.5,
    ) -> JsonCorrectnessMetric:
        """Whether `actual_output` is valid JSON conforming to `expected_schema` (a pydantic model)."""
        return self._measure(
            JsonCorrectnessMetric(expected_schema=expected_schema, model=self.model, threshold=threshold),
            test_case,
        )

    def prompt_alignment(
        self,
        test_case: LLMTestCase,
        prompt_instructions: List[str],
        threshold: float = 0.5,
    ) -> PromptAlignmentMetric:
        """Whether `actual_output` follows every instruction in `prompt_instructions`."""
        return self._measure(
            PromptAlignmentMetric(prompt_instructions=prompt_instructions, threshold=threshold, model=self.model),
            test_case,
        )

    def exact_match(self, test_case: LLMTestCase, threshold: float = 1.0) -> ExactMatchMetric:
        """Non-LLM: whether `actual_output` exactly equals `expected_output` (after stripping)."""
        return self._measure(ExactMatchMetric(threshold=threshold), test_case)

    def pattern_match(
        self,
        test_case: LLMTestCase,
        pattern: str,
        ignore_case: bool = False,
        threshold: float = 1.0,
    ) -> PatternMatchMetric:
        """Non-LLM: whether `actual_output` fully matches the regex `pattern`."""
        return self._measure(PatternMatchMetric(pattern=pattern, ignore_case=ignore_case, threshold=threshold), test_case)

    # Non-LLM statistical scoring (deepeval.scorer.Scorer
    
    def rouge_score(self, target: str, prediction: str, score_type: str = "rougeL") -> float:
        """N-gram recall/overlap between `prediction` and `target`. score_type: "rouge1", "rouge2" or "rougeL"."""
        return self._scorer.rouge_score(target=target, prediction=prediction, score_type=score_type)

    def sentence_bleu_score(self, references: Union[str, List[str]], prediction: str, bleu_type: str = "bleu1") -> float:
        """N-gram precision of `prediction` against one or more `references`. bleu_type: "bleu1".."bleu4"."""
        return self._scorer.sentence_bleu_score(references=references, prediction=prediction, bleu_type=bleu_type)

    def exact_match_score(self, target: str, prediction: str) -> int:
        """1 if `prediction` equals `target` exactly (after stripping), else 0."""
        return self._scorer.exact_match_score(target=target, prediction=prediction)

    def quasi_exact_match_score(self, target: str, prediction: str) -> int:
        """1 if `prediction` equals `target` after normalizing case/punctuation/whitespace, else 0."""
        return self._scorer.quasi_exact_match_score(target=target, prediction=prediction)

    def quasi_contains_score(self, targets: List[str], prediction: str) -> int:
        """1 if normalized `prediction` matches any normalized string in `targets`, else 0."""
        return self._scorer.quasi_contains_score(targets=targets, prediction=prediction)

    def bert_score(
        self,
        references: Union[str, List[str]],
        predictions: Union[str, List[str]],
        model: str = "microsoft/deberta-large-mnli",
        lang: str = "en",
    ) -> float:
        """Embedding-based semantic similarity between `predictions` and `references` (BERTScore)."""
        return self._scorer.bert_score(references=references, predictions=predictions, model=model, lang=lang)
