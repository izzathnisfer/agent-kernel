"""Enablement checks and construction of the configured ``Scheduler``.

The two entry points are deliberately split: ``validate_config()`` is pure config
validation that never touches AWS, so a misconfiguration surfaces at component
initialization rather than at the first ``create_schedule`` call.
"""

import logging
import threading
from typing import TYPE_CHECKING, Optional

from ..core.config import AKConfig
from ..core.util.factory import AKConfigError
from .base import Scheduler
from .store.base import DURABLE_SESSION_TYPES

if TYPE_CHECKING:
    from .service import ScheduledTaskService

# session.type -> (config block, field, whether the deployment must supply it). A DynamoDB
# table name can't be guessed, so it's required; Redis/Valkey reuse the session cluster's URL
# with a constant prefix, so neither needs a supplied value.
_REQUIRED_BACKEND_FIELD = {
    "dynamodb": ("dynamodb", "table_name", True),
    "redis": ("redis", "prefix", False),
    "valkey": ("valkey", "prefix", False),
}


class SchedulerFactory:
    """Resolves the scheduled-task capability from ``AKConfig``."""

    _log = logging.getLogger("ak.scheduler.factory")
    _instance: Optional[Scheduler] = None
    # Reentrant for symmetry with the other process-wide singletons in this codebase.
    _lock = threading.RLock()

    @staticmethod
    def enabled() -> bool:
        """Whether a scheduler block is present and switched on.

        :return: True when scheduled tasks are enabled for this deployment.
        """
        # getattr, not attribute access: an older config object must not raise here.
        config = getattr(AKConfig.get(), "scheduler", None)
        return bool(config and config.enabled)

    @staticmethod
    def validate_config() -> None:
        """Enforce every precondition the capability depends on, without touching AWS.

        Called by each scheduler-enabled component at initialization — process startup on
        ECS, cold start on serverless — so a non-viable deployment fails loudly rather than
        silently.

        :raises AKConfigError: A precondition is not met.
        """
        if not SchedulerFactory.enabled():
            return

        config = AKConfig.get()
        SchedulerFactory._validate_session_type(config)
        SchedulerFactory._validate_timer_wiring(config)
        SchedulerFactory._validate_backend_block(config)

    @staticmethod
    def build() -> Scheduler:
        """Return the process-wide ``Scheduler``, constructing it on first use.

        Memoized because construction creates boto3 clients and the store — work that
        belongs once per process, not once per request.

        :return: The configured scheduler.
        :raises AKConfigError: A precondition is not met.
        """
        if SchedulerFactory._instance is None:
            with SchedulerFactory._lock:
                if SchedulerFactory._instance is None:
                    SchedulerFactory.validate_config()
                    from .providers.aws import AWSSchedulerBuilder

                    SchedulerFactory._instance = AWSSchedulerBuilder.build()
        return SchedulerFactory._instance

    @classmethod
    def _reset(cls) -> None:
        """Clear the memoized scheduler so the next build() reconstructs it (tests only)."""
        with cls._lock:
            cls._instance = None

    # ------------------------------------------------------------------ checks

    @staticmethod
    def _validate_session_type(config: AKConfig) -> None:
        """Reject a session backend too volatile to share scheduled tasks across replicas.

        :param config: The loaded configuration.
        :raises AKConfigError: ``session.type`` is not a durable backend.
        """
        if config.session.type.lower() not in DURABLE_SESSION_TYPES:
            raise AKConfigError(
                f"scheduler.enabled requires a durable session store; session.type '{config.session.type}' "
                f"is not one of {list(DURABLE_SESSION_TYPES)}. The scheduled-task table follows the session store type."
            )

    @staticmethod
    def _validate_timer_wiring(config: AKConfig) -> None:
        """Reject scheduling enabled in YAML without the deployment values Terraform injects.

        An empty string counts as unset: the examples declare these as ``""`` placeholders
        that Terraform fills, so a deployment missing the wiring must fail here rather than
        at the first registration.

        :param config: The loaded configuration.
        :raises AKConfigError: ``group_name`` or ``target_role_arn`` is missing.
        """
        for field in ("group_name", "target_role_arn"):
            if not (getattr(config.scheduler, field) or "").strip():
                raise AKConfigError(
                    f"scheduler.{field} is required when scheduler.enabled is true; "
                    f"it is injected by Terraform via AK_SCHEDULER__{field.upper()}."
                )

    @staticmethod
    def _validate_backend_block(config: AKConfig) -> None:
        """Reject a scheduler store block that contradicts the resolved session type.

        The store follows ``session.type`` and is never selected separately, so the only
        failures possible here are a deployment value that was not injected and a block for a
        backend that will never be read.

        :param config: The loaded configuration.
        :raises AKConfigError: A required deployment value is missing, a declared value is
            blank, or a block for another backend is set.
        """
        session_type = config.session.type.lower()
        required_block, required_field, deployment_supplied = _REQUIRED_BACKEND_FIELD[session_type]

        block = getattr(config.scheduler, required_block, None)
        if deployment_supplied and block is None:
            raise AKConfigError(
                f"session.type is '{session_type}', so scheduler.{required_block}.{required_field} must be set "
                f"for the scheduled-task store; it is injected by Terraform via "
                f"AK_SCHEDULER__{required_block.upper()}__{required_field.upper()}."
            )
        # A declared-but-blank value is a wiring failure, not a request for the default — the
        # examples ship "" placeholders for Terraform to fill.
        if block is not None and not (getattr(block, required_field) or "").strip():
            raise AKConfigError(
                f"scheduler.{required_block}.{required_field} is declared but empty. Remove it to accept the "
                f"default, or supply a value (Terraform injects AK_SCHEDULER__{required_block.upper()}__{required_field.upper()})."
            )

        for other_block in _REQUIRED_BACKEND_FIELD:
            if other_block != required_block and getattr(config.scheduler, other_block, None) is not None:
                raise AKConfigError(
                    f"scheduler.{other_block} is configured but session.type is '{session_type}', so it would never be read. "
                    f"The scheduled-task store follows the session store. Remove it, or set session.type to '{other_block}'."
                )

    @staticmethod
    def service() -> Optional["ScheduledTaskService"]:
        """Return a ``ScheduledTaskService`` over the configured scheduler, or None when disabled.

        The one place the create paths and route layers obtain the service, so no surface
        has to repeat the enabled check plus construction.

        :return: The service, or None when scheduling is disabled for this deployment.
        """
        if not SchedulerFactory.enabled():
            return None
        from .service import ScheduledTaskService

        return ScheduledTaskService(SchedulerFactory.build())
