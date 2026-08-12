#!/bin/bash
set -eo pipefail # exit if any command in this script fails

create_request_handler_deployment_package() {
	echo "Creating request handler deployment package..."
	pushd ../
	rm -rf dist_request_handler dist_request_handler.zip
	mkdir -p dist_request_handler
	uv export --extra request_handler --no-hashes >requirements.txt
	if [[ ${1-} != "local" ]]; then
		uv pip install -r requirements.txt --target=dist_request_handler
	else
		uv pip install --force-reinstall --target=dist_request_handler --find-links ../../../ak-py/dist agentkernel[aws] --no-cache-dir
	fi
	cp -r lambda_request_handler.py config.yaml dist_request_handler/
	cd dist_request_handler && zip -rq ../dist_request_handler.zip .
	popd || exit 1
}

# Create agent runner lambda deployment package
# Terraform builds this directory into a container image and pushes it to an ECR
# repository it manages, so all this needs to produce is the build context.
create_agent_runner_deployment_package() {
	echo "Creating agent runner deployment package..."
	pushd ../
	rm -rf dist_agent_runner
	mkdir -p dist_agent_runner/data
	uv export --extra agent_runner --no-hashes >requirements.txt
	if [[ ${1-} != "local" ]]; then
		uv pip install -r requirements.txt --target=dist_agent_runner/data
	else
		uv pip install --force-reinstall --target=dist_agent_runner/data --find-links ../../../ak-py/dist agentkernel[aws,openai] --no-cache-dir
	fi
	cp -r lambda_agent_runner.py config.yaml dist_agent_runner/data
	popd || exit 1
	cp Dockerfile.agent_runner ../dist_agent_runner/Dockerfile
}

create_response_handler_deployment_package() {
	echo "Creating response handler deployment package..."
	pushd ../
	rm -rf dist_response_handler dist_response_handler.zip
	mkdir -p dist_response_handler
	uv export --extra response_handler --no-hashes >requirements.txt
	if [[ ${1-} != "local" ]]; then
		uv pip install -r requirements.txt --target=dist_response_handler
	else
		uv pip install --force-reinstall --target=dist_response_handler --find-links ../../../ak-py/dist agentkernel[aws] --no-cache-dir
	fi
	cp -r lambda_response_handler.py config.yaml dist_response_handler/
	cd dist_response_handler && zip -rq ../dist_response_handler.zip .
	popd || exit 1
}

# The authorizer resolves the user id that owns a scheduled task, so it ships as its own
# small Lambda. It validates a token and returns a policy — no agent frameworks needed.
create_auth_deployment_package() {
	echo "Creating authorizer deployment package..."
	pushd ../
	rm -rf dist_auth dist_auth.zip
	mkdir -p dist_auth
	if [[ ${1-} != "local" ]]; then
		uv pip install --force-reinstall --no-deps agentkernel[aws,auth] --target=dist_auth
	else
		uv pip install --force-reinstall --no-deps --no-index agentkernel[aws,auth] --target=dist_auth --find-links ../../../ak-py/dist
	fi
	uv pip install --group auth --target=dist_auth
	cp -r lambda_auth.py config.yaml dist_auth/
	cd dist_auth && zip -rq ../dist_auth.zip .
	popd || exit 1
}

create_request_handler_deployment_package $1
create_agent_runner_deployment_package $1
create_response_handler_deployment_package $1
create_auth_deployment_package $1

terraform init
terraform apply
