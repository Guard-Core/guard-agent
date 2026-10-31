Contributing to Guard Agent
===================================

We appreciate your interest in contributing to Guard Agent. This document provides comprehensive guidelines for participating in the development of this enterprise-grade security telemetry solution, ensuring efficient collaboration and maintaining our high standards of code quality.

Code of Conduct
---------------

This project maintains strict adherence to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). All contributors are expected to uphold these professional standards. Any violations should be reported directly to the project maintainers for immediate review.

___

Contribution Guidelines
-----------------------

Reporting Security Vulnerabilities and Bugs
-------------------------------------------

Prior to submitting bug reports, conduct a thorough search of existing issues to prevent duplicates. When documenting a bug, provide comprehensive details:

- Utilize precise, descriptive titles that clearly identify the issue
- Document exact reproduction steps with minimal test cases
- Include specific examples, including relevant HTTP requests and payloads
- Articulate observed behavior versus expected behavior with technical precision
- Provide complete error logs, stack traces, and diagnostic information
- Detail your execution environment: operating system, Python version, framework adapter (fastapi-guard / flaskapi-guard / djangoapi-guard / tornadoapi-guard) and its version, and all relevant dependencies

Proposing Feature Enhancements
-------------------------------

Feature proposals are managed through GitHub's issue tracking system. When submitting enhancement requests:

- Craft concise yet comprehensive titles that encapsulate the feature scope
- Provide detailed technical specifications for the proposed functionality
- Articulate the business value and technical benefits for the Guard ecosystem's user base
- Include practical implementation examples and use case scenarios
- Reference relevant industry standards, competing implementations, or academic research

Pull Request Standards
----------------------

- Complete all sections of the pull request template comprehensively
- Maintain strict adherence to PEP 8 and project-specific style conventions
- Implement comprehensive test coverage for all new functionality and bug fixes
- Update technical documentation to reflect architectural changes
- Verify full test suite execution without failures or regressions
- Ensure static analysis tools (mypy, ruff) report zero violations

Development Environment Configuration
-------------------------------------

1. Fork the repository and establish a local development clone

2. This project leverages containerization for consistent development environments. Ensure Docker and Docker Compose are properly installed and configured.

3. Utilize the comprehensive Makefile automation for environment initialization:

```bash
# Install dependencies using 'uv'
make install

# To stop all containers
make stop
```

Testing Infrastructure
----------------------

The project maintains compatibility across Python 3.10, 3.11, 3.12, 3.13, 3.14, and 3.15. Our containerized testing infrastructure ensures consistency across environments:

```bash
# Run tests with the default Python version (3.10)
make test

# Run tests with all supported Python versions
make test-all

# Run tests with a specific Python version
make test-3.11

# Run tests locally (if you have 'uv' installed)
make local-test
```

Code Quality Standards
----------------------

This project enforces rigorous code quality through:
- [Ruff](https://github.com/astral-sh/ruff) for high-performance code formatting and linting
- [mypy](https://mypy.readthedocs.io/) for comprehensive static type analysis

Prior to pull request submission, validate compliance with all quality standards:

```bash
make lint
make fix
```

and

```bash
make lint-docs
make fix-docs
```

Documentation Standards
-----------------------

Technical documentation is generated using MkDocs with Material theme. To build and preview documentation locally:

```bash
make serve-docs
```

Docs build only on Python 3.14 (`make serve-docs` and the docs workflow use it): on 3.10 the newest mkdocstrings (1.0.6) fails with pymdown-extensions 12 with `TypeError: Highlight.__init__() missing 1 required positional argument: 'md'`.

Comprehensive documentation updates are mandatory for all significant architectural or API modifications.

___

Version Management
------------------

This project strictly adheres to [Semantic Versioning](https://semver.org/) principles.

Release Engineering Process
---------------------------

1. Update version in `pyproject.toml` and `setup.py`
2. Update `docs/release-notes.md`
3. Create a new GitHub release with release notes
4. CI will automatically publish to PyPI

___

Support and Communication
-------------------------

For technical inquiries regarding the development process or architectural decisions, please initiate a discussion through the issue tracking system.

Your contributions are vital to the continued evolution of Guard Agent's security capabilities.
