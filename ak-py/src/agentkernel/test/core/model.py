from enum import StrEnum


class Mode(StrEnum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    OVERLAP = "overlap"
    SEMANTIC = "semantic"
    JUDGE = "judge"
    SAFETY = "safety"
    STRUCTURAL = "structural"
    HUMAN = "human"
    FALLBACK = "fallback"
