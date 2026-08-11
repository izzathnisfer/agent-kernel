from typing import Optional

from agentkernel.auth import AuthValidator, ValidationContext, ValidationResult
from agentkernel.aws import APIGatewayAuthorizer


class DemoAuthValidator(AuthValidator):
    """
    Demo API Gateway authorizer resolving a Bearer token to the user id that owns a
    scheduled task.

    A real validator would verify a JWT signature against your identity provider (the
    _validate_rs256_jwt helper on the base class does this) and read the user id from a
    claim. Here a static token map stands in for that provider.

    `subject` is the load-bearing field: API Gateway surfaces it as `principalId`, which
    the schedule routes read as the scheduled task's owner. Returning `is_valid=True`
    without a subject leaves it at its "user" default, which would make every caller share
    one identity — and therefore one another's scheduled tasks.
    """

    _TOKENS = {
        "alice-token": "alice",
        "bob-token": "bob",
    }

    def validate(self, token: str, context: Optional[ValidationContext] = None) -> ValidationResult:
        user_id = self._TOKENS.get(token)
        if user_id is None:
            return ValidationResult(is_valid=False, error_msg="Unknown token")
        return ValidationResult(is_valid=True, subject=user_id)


handler = APIGatewayAuthorizer(validator=DemoAuthValidator()).handle
