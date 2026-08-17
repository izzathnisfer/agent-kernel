from enum import StrEnum


class Mode(StrEnum):
    FUZZY = "fuzzy"
    SEMANTIC = "semantic"
    JUDGE = "judge"
    FALLBACK = "fallback"
