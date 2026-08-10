import datetime
import json

import pytest
from pydantic import BaseModel, ValidationError

from agentkernel.core.model import AgentReply, AgentReplyAny, BaseRunRequest, ScheduledRunMetadata, ScheduleMode, ScheduleSpec


class WeatherReport(BaseModel):
    city: str
    temp_c: int
    observed_at: datetime.datetime


class TestAgentReplyAny:
    """Test construction, serialization and __str__ of AgentReplyAny"""

    def test_construction_and_defaults(self):
        reply = AgentReplyAny(content={"a": 1})

        assert reply.content == {"a": 1}
        assert reply.prompt == ""
        assert reply.type == "other"

    def test_str_returns_json(self):
        content = {"city": "Colombo", "temp_c": 31}
        reply = AgentReplyAny(content=content)

        assert str(reply) == json.dumps(content)
        # Must be parseable JSON, not a Python repr
        assert json.loads(str(reply)) == content

    def test_serialization(self):
        reply = AgentReplyAny(content={"k": "v"}, prompt="the prompt")

        dumped = reply.model_dump()
        assert dumped == {"content": {"k": "v"}, "prompt": "the prompt", "type": "other"}
        assert json.loads(reply.model_dump_json()) == dumped

    def test_non_dict_content_raises(self):
        with pytest.raises(ValidationError):
            AgentReplyAny(content="not a dict")


class TestAgentReplyAnyFromOutput:
    """Test the from_output classmethod used by framework runners"""

    def test_pydantic_instance_converted_via_model_dump_json_mode(self):
        model = WeatherReport(city="Colombo", temp_c=31, observed_at=datetime.datetime(2026, 7, 8, 12, 0))

        reply = AgentReplyAny.from_output(model, "weather?")

        assert isinstance(reply, AgentReplyAny)
        assert reply.prompt == "weather?"
        # mode="json" serializes the datetime, so str(reply) cannot fail
        assert reply.content == {"city": "Colombo", "temp_c": 31, "observed_at": "2026-07-08T12:00:00"}
        assert json.loads(str(reply)) == reply.content

    def test_dict_used_as_content_directly(self):
        content = {"k": "v"}

        reply = AgentReplyAny.from_output(content)

        assert isinstance(reply, AgentReplyAny)
        assert reply.content == content
        assert reply.prompt == ""

    def test_unstructured_values_return_none(self):
        assert AgentReplyAny.from_output("plain text") is None
        assert AgentReplyAny.from_output(42) is None
        assert AgentReplyAny.from_output(None) is None
        assert AgentReplyAny.from_output(["a", "b"]) is None


class TestScheduleSpec:
    """The `schedule` block on a chat body."""

    def test_exactly_one_timing_expression_is_required(self):
        with pytest.raises(ValidationError):
            ScheduleSpec()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"cron": "0 9 * * ? *", "rate": "1 hour"},
            {"rate": "1 hour", "at": "2026-08-09T09:00:00Z"},
            {"cron": "0 9 * * ? *", "rate": "1 hour", "at": "2026-08-09T09:00:00Z"},
        ],
    )
    def test_multiple_timing_expressions_are_rejected(self, kwargs):
        with pytest.raises(ValidationError):
            ScheduleSpec(**kwargs)

    def test_per_run_is_the_default_mode(self):
        assert ScheduleSpec(rate="1 hour").mode == ScheduleMode.PER_RUN


class TestBaseRunRequestSchedulingFields:
    """Both fields are optional, so every existing caller is unaffected."""

    def test_both_fields_default_to_none(self):
        request = BaseRunRequest(prompt="hi")
        assert request.schedule is None
        assert request.scheduled_run is None

    def test_both_fields_round_trip(self):
        payload = {
            "prompt": "hi",
            "schedule": {"rate": "1 hour", "mode": "continuous"},
            "scheduled_run": {
                "scheduled_task_id": "a",
                "scheduled_task_version": "v1",
                "scheduled_time": "2026-08-09T09:00:00Z",
                "run_id": "exec-1",
            },
        }
        request = BaseRunRequest.model_validate(payload)
        assert request.schedule.mode == ScheduleMode.CONTINUOUS
        assert request.scheduled_run.scheduled_task_id == "a"
        assert BaseRunRequest.model_validate(request.model_dump(mode="json")) == request


class TestScheduledRunMetadataExtraction:
    """Two extraction paths: the consumers' hot path and the runners' failure path."""

    VALID = {
        "scheduled_task_id": "a",
        "scheduled_task_version": "v1",
        "scheduled_time": "2026-08-09T09:00:00Z",
        "run_id": "exec-1",
    }

    def test_from_body_returns_none_on_the_common_miss(self):
        assert ScheduledRunMetadata.from_body({"result": "hi"}) is None

    def test_from_body_parses_a_present_block(self):
        assert ScheduledRunMetadata.from_body({"scheduled_run": self.VALID}).run_id == "exec-1"

    def test_from_body_surfaces_a_malformed_block(self):
        """On the ordinary consumer path a malformed block is a real bug worth surfacing."""
        with pytest.raises(ValidationError):
            ScheduledRunMetadata.from_body({"scheduled_run": {"scheduled_task_id": "a"}})

    @pytest.mark.parametrize("raw", ["{not json", None, "[]", json.dumps({"result": "hi"}), json.dumps({"scheduled_run": {"bad": 1}})])
    def test_from_raw_body_never_raises(self, raw):
        assert ScheduledRunMetadata.from_raw_body(raw) is None

    @pytest.mark.parametrize("raw", [json.dumps({"scheduled_run": VALID}), {"scheduled_run": VALID}])
    def test_from_raw_body_parses_a_string_or_a_dict(self, raw):
        assert ScheduledRunMetadata.from_raw_body(raw).run_id == "exec-1"


class TestImportDirection:
    """core/ must import nothing from scheduler/, so the dependency points one way."""

    def test_importing_core_model_pulls_in_no_scheduler_module(self):
        import subprocess
        import sys

        script = "import sys; import agentkernel.core.model; " "print(any(name.startswith('agentkernel.scheduler') for name in sys.modules))"
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert result.stdout.strip().splitlines()[-1] == "False", result.stderr

    def test_the_capability_re_exports_the_very_same_classes(self):
        from agentkernel.scheduler.model import ScheduleSpec as ReExported

        assert ReExported is ScheduleSpec
