# Developer Guide

This guide provides essential information for developers working on the Agent Kernel project.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Development Setup](#development-setup)
- [Makefile Commands](#makefile-commands)
- [Code Quality](#code-quality)
- [Project Structure](#project-structure)
- [Contributing](#contributing)

## Prerequisites

Before you begin development, ensure you have the following installed:

- Python 3.12 or higher
- [uv](https://github.com/astral-sh/uv) - Fast Python package installer and resolver
- Git
- Make

## Setup Your Development Environment

1. **Fork the repository** on GitHub

2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/yaalalabs/agent-kernel.git
   cd agent-kernel
   ```

3. **Add the upstream repository**:
   ```bash
   git remote add upstream https://github.com/yaalalabs/agent-kernel.git
   ```

4. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```


## Development

1. **Run the dev setup script** from the repo root to install prerequisites (pyenv, Python 3.12, uv) and sync the `ak-py` virtual environment:
   ```bash
   make dev-setup
   ```
   or directly:
   ```bash
   ./scripts/dev-setup.sh
   ```

   Alternatively, set things up manually:
   ```bash
   cd ak-py
   ./build.sh
   ```

## Makefile Commands

The project includes a Makefile with several useful commands for code formatting and quality checks. All commands should be run from the root directory of the project.

### Available Commands

To see all available Makefile commands:
```bash
make help
```
## Code Quality

### Formatting Standards

Agent Kernel uses the following tools to maintain code quality:

- **[black](https://github.com/psf/black)**: Opinionated code formatter
- **[isort](https://pycqa.github.io/isort/)**: Import statement organizer

### Pre-commit Workflow

Before committing code, run:
```bash
make lint-check-all
```

This ensures your code meets the project's formatting standards without making changes. If issues are found, run:
```bash
make lint-all
```

to automatically fix formatting issues.


## Contributing

### Development Workflow

1. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Write code following the project's conventions
   - Add tests for new functionality

3. **Verify formatting**
   ```bash
   make lint-check-all
   ```

4. **Format your code**
   ```bash
   make lint-all
   ```

5. **Run tests**
   ```bash
   cd ak-py
   uv run pytest
   ```

6. **Commit your changes**
   ```bash
   git add .
   git commit -m "feat: describe your changes"
   ```

7. **Push to your branch**
   ```bash
   git push origin feature/your-feature-name
   ```

### Commit Message Convention

Follow conventional commit format:
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `chore:` - Maintenance tasks
- `refactor:` - Code refactoring
- `test:` - Test additions or modifications

### Code Review

- Ensure all formatting checks pass
- Add appropriate tests
- Ensure all CI tests pass
- Update documentation if needed
- Request review from maintainers

### Adding Tests to CI and the Integration Status Page

E2E and integration tests are driven by two config files:

- [`.github/test-config.yaml`](.github/test-config.yaml) — e2e tests run on every PR and push to `develop`
- [`.github/integration-test-config.yaml`](.github/integration-test-config.yaml) — nightly and weekly integration tests

When adding a test entry, include a `dashboard` block so the integration shows
up correctly on the public [Integration Status page](https://kernel.yaala.ai/status):

```yaml
- type: api
  path: examples/api/my-new-example
  dashboard:
    category: Messaging Integrations   # dashboard section
    label: My New Example              # tile name, unique within the category
```

A list of `dashboard` blocks surfaces one test as multiple tiles;
`dashboard: hidden` opts out. Validate with
`python3 .github/scripts/validate_integration_config.py`. See
[`.github/INTEGRATION_TESTS.md`](.github/INTEGRATION_TESTS.md) and
[`docs/specs/integration-status.md`](docs/specs/integration-status.md).

## Additional Resources

- [Main README](README.md) - Project overview and usage
- [Documentation Setup](docs/SETUP.md) - Setting up the documentation site
- [Examples](examples/) - Sample implementations
- [Use Cases](use-cases/) - End-to-end agents built from `SPEC.md` using Agent Kernel skills
