from abc import ABC, abstractmethod
from typing import ClassVar

from ..model import Mode


class Evaluator(ABC):

    mode_to_functionname_mapping: ClassVar[dict[Mode, str]] = {
        Mode.FUZZY: "fuzzy",
        Mode.JUDGE: "judge",
    }

    @abstractmethod
    def fuzzy(self):
        pass

    @abstractmethod
    def judge(self):
        pass

    @staticmethod
    def get_function_name_from_mode(mode: Mode) -> str:
        return Evaluator.mode_to_functionname_mapping[mode]


