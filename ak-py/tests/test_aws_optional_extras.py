"""
``agentkernel.aws`` must import without the optional ``api`` extra.

The serverless Lambda handlers install ``agentkernel[aws,...]`` with no ``api`` extra and serve no
routes of their own, so a FastAPI import anywhere on the ``agentkernel.aws`` path kills them at
init with ``Runtime.ImportModuleError``, before a single request is handled. The containerized
package re-exports the FastAPI-backed API classes lazily to keep that path clean.
"""

import subprocess
import sys

import pytest

from agentkernel.deployment.aws.containerized.core.api.websocket_api import AWSWebsocketAPI


def _run(code: str) -> subprocess.CompletedProcess:
    """
    Run a snippet in a fresh interpreter.
    :param code: Python source to execute.
    :return: The completed process.
    """
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


# Each Lambda handler's actual first import — see examples/aws-serverless/*/lambda_*.py.
LAMBDA_ENTRYPOINTS = ["Lambda", "ServerlessAgentRunner", "ResponseHandler"]


@pytest.mark.parametrize("name", LAMBDA_ENTRYPOINTS)
def test_lambda_entrypoint_import_does_not_pull_in_fastapi(name):
    # A fresh interpreter is the only honest check: fastapi is installed in the dev env, so an
    # eager import succeeds here and fails only on a deployment without the `api` extra.
    result = _run(f"import sys\nfrom agentkernel.aws import {name}\nassert 'fastapi' not in sys.modules, '{name} pulled in fastapi'\n")
    assert result.returncode == 0, result.stderr


def test_api_classes_still_resolve_from_the_package_root():
    from agentkernel.aws import AWSWebsocketAPI as lazily_exported

    assert lazily_exported is AWSWebsocketAPI


def test_api_classes_stay_discoverable():
    # __getattr__ alone hides them from dir(), which reads the module dict.
    import agentkernel.aws as aws_package

    assert {"AWSRestAPI", "AWSWebsocketAPI", "ECSWebSocketSystemRequestHandler"} <= set(dir(aws_package))


def test_unknown_attribute_raises_attribute_error():
    import agentkernel.aws as aws_package

    with pytest.raises(AttributeError):
        aws_package.NoSuchName
