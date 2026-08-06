#!/usr/bin/env python3
"""
Retrieve network outputs from a base deployment.

This script initializes Terraform in a base deployment directory and retrieves
the network identifiers other example deployments reuse (so the e2e harness
deploys the VPC once instead of per example). Results are written to
$GITHUB_OUTPUT for use in subsequent workflow steps.

- AWS (default): reads `vpc_id` (raw) and `private_subnet_ids` (JSON array) and
  writes `vpc_id` / `private_subnet_ids`.
- GCP: reads `network_id` and `private_subnet_id` (both raw) and writes
  `gcp_network_id` / `gcp_private_subnet_id` (so they don't collide with the
  AWS keys when both bases are read in the same job).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _tf_output(deploy_path: Path, name: str, raw: bool = True) -> str:
    """Return a single terraform output value."""
    flag = "-raw" if raw else "-json"
    result = subprocess.run(
        ["terraform", "output", flag, name],
        cwd=str(deploy_path),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_outputs(pairs: dict) -> None:
    for key, value in pairs.items():
        print(f"{key}: {value}")
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            for key, value in pairs.items():
                f.write(f"{key}={value}\n")
        print("Outputs written to $GITHUB_OUTPUT")
    else:
        print("GITHUB_OUTPUT not set — printing outputs only")


def main():
    parser = argparse.ArgumentParser(
        description="Retrieve base deployment network outputs"
    )
    parser.add_argument(
        "--base-path",
        default="examples/aws-serverless/openai",
        help="Path to the base deployment project",
    )
    parser.add_argument(
        "--deploy-dir",
        default="deploy",
        help="Deploy directory within the base path",
    )
    parser.add_argument(
        "--cloud",
        choices=["aws", "gcp"],
        default="aws",
        help="Which cloud's base outputs to read",
    )
    args = parser.parse_args()

    deploy_path = Path(args.base_path) / args.deploy_dir

    if not deploy_path.exists():
        print(f"Error: Base deployment path not found: {deploy_path}")
        sys.exit(1)

    # Initialize Terraform to access remote state
    print(f"Initializing Terraform in {deploy_path}...")
    subprocess.run(
        ["terraform", "init", "-upgrade"],
        cwd=str(deploy_path),
        check=True,
        env={**os.environ, "TF_INPUT": "0"},
    )

    if args.cloud == "aws":
        vpc_id = _tf_output(deploy_path, "vpc_id", raw=True)
        private_subnet_ids = _tf_output(deploy_path, "private_subnet_ids", raw=False)
        json.loads(private_subnet_ids)  # ensure valid JSON
        _write_outputs(
            {"vpc_id": vpc_id, "private_subnet_ids": private_subnet_ids}
        )
    else:  # gcp
        network_id = _tf_output(deploy_path, "network_id", raw=True)
        private_subnet_id = _tf_output(deploy_path, "private_subnet_id", raw=True)
        _write_outputs(
            {
                "gcp_network_id": network_id,
                "gcp_private_subnet_id": private_subnet_id,
            }
        )


if __name__ == "__main__":
    main()
