---
name: ak-dev-code-patterns
description: >
  Python coding practices for Agent Kernel development — class-based design over free functions,
  dependency injection, small focused methods, naming, type hints, comments/docstrings, error
  handling, avoiding deep nesting/magic values/duplication/global state, immutability, logging,
  and testing conventions. Use this skill when designing, implementing, reviewing, or refactoring
  Python code in this repo.
license: Apache-2.0
metadata:
  author: yaalalabs
  category: developer
---

# Python Best Programming Practices

## Purpose

This skill defines the Python programming practices that should be followed when designing,
implementing, reviewing, and refactoring Python code in Agent Kernel.

The primary goals are:

* Maintainable and readable code
* Clear ownership and encapsulation
* Strong object-oriented design where appropriate
* Easy testing and mocking
* Consistent error handling
* Minimal duplication
* Clear documentation
* Code that can be understood by developers who did not originally write it

---

## Inputs

When applying this skill, consider the following inputs:

1. **Existing code**
   * Classes and their responsibilities
   * Existing interfaces and abstractions
   * Existing coding conventions
   * Existing dependency patterns

2. **Feature or change requirements**
   * What functionality needs to be implemented
   * Expected inputs and outputs
   * Error and edge-case behavior
   * Backward-compatibility requirements

3. **Architecture**
   * Existing modules and layers
   * Framework conventions
   * Dependency injection patterns
   * Existing abstract classes/interfaces

4. **Tests**
   * Existing unit tests
   * Integration tests
   * Expected test coverage
   * Existing mocking/fixture patterns

5. **Project configuration**
   * Python version
   * `pyproject.toml`
   * Linting configuration
   * Formatting configuration
   * Type-checking configuration

6. **Review context**
   * Whether the code is new or existing
   * Whether the change is a refactor or feature
   * Performance, scalability, or compatibility constraints

---

# Core Principles

## 1. Prefer Classes Over Standalone Functions

Do not automatically create top-level/open functions.

When functionality belongs to a particular responsibility, prefer implementing it inside an
appropriate class.

### Prefer

```python
class TaskScheduler:
    def calculate_next_run(self, task):
        ...
```

Instead of:

```python
def calculate_next_run(task):
    ...
```

This provides:

* Clear ownership
* Better encapsulation
* Easier dependency management
* Easier mocking and testing
* Better extensibility
* A natural place for related functionality

### When a standalone function is acceptable

A standalone function can be used when it is genuinely:

* Stateless
* Generic
* Independent of application/domain state
* A small utility with no meaningful class ownership

For example:

```python
def parse_version(version: str) -> tuple[int, int, int]:
    ...
```

Do not create a class merely to wrap trivial utility functions.

### Abstract behavior

If a behavior needs to be implemented differently by multiple components, define it as an
abstract method.

```python
from abc import ABC, abstractmethod


class TaskStore(ABC):

    @abstractmethod
    def get_task(self, task_id: str):
        """Return a task by ID."""
        raise NotImplementedError
```

Concrete implementations then provide the behavior:

```python
class DynamoTaskStore(TaskStore):

    def get_task(self, task_id: str):
        ...
```

### Rule

Before creating a standalone function, ask:

> "Does this behavior belong to an existing object or abstraction?"

If yes, implement it as a method.

---

## 2. Keep Classes Focused

A class should have a clear responsibility.

Avoid classes that become large collections of unrelated functionality.

### Bad

```python
class TaskManager:
    def create_task(self):
        ...

    def send_email(self):
        ...

    def generate_report(self):
        ...

    def upload_to_s3(self):
        ...

    def validate_user(self):
        ...
```

### Better

Separate responsibilities:

```python
class TaskManager:
    ...

class EmailService:
    ...

class ReportGenerator:
    ...

class StorageService:
    ...

class UserValidator:
    ...
```

A class should be easy to describe in one sentence.

---

## 3. Use Dependency Injection

Do not unnecessarily create dependencies inside business classes.

### Avoid

```python
class TaskService:

    def __init__(self):
        self.store = DynamoTaskStore()
        self.logger = Logger()
```

Prefer:

```python
class TaskService:

    def __init__(self, store, logger):
        self.store = store
        self.logger = logger
```

This improves:

* Testing
* Reusability
* Dependency replacement
* Configuration
* Separation of concerns

---

## 4. Write Small, Focused Methods

Methods should perform one clear responsibility.

Avoid methods that parse input, validate input, access a database, transform data, call an API,
format output, and handle unrelated errors all in one large method.

Prefer decomposition:

```python
class TaskService:

    def create_task(self, request):
        task = self._validate_request(request)
        task = self._build_task(task)
        self._store_task(task)
        return task
```

Private methods can represent implementation details.

---

## 5. Naming

Names should explain intent.

### Prefer

```python
task_repository
scheduled_task
next_execution_time
```

### Avoid

```python
x
data
obj
tmp
res
val
```

Unless the scope is extremely small and the meaning is obvious.

Use verbs for actions:

```python
create_task()
delete_task()
calculate_next_run()
validate_request()
```

Use nouns for objects:

```python
Task
TaskStore
TaskScheduler
ExecutionResult
```

---

## 6. Type Hints

Use type hints for public interfaces and important internal code.

### Prefer

```python
def get_task(self, task_id: str) -> Task | None:
    ...
```

Instead of:

```python
def get_task(self, task_id):
    ...
```

Use explicit types for function arguments, return values, class attributes where useful, and
complex data structures.

Type hints should make the code easier to understand, not unnecessarily complicated.

---

## 7. Comments

Comments should explain **why**, not simply repeat **what** the code does.

### Bad

```python
# Increment counter by one
counter += 1
```

This provides no additional information.

### Good

```python
# Retry count starts at zero because the initial execution is not a retry.
retry_count = 0
```

The comment explains the reasoning.

### Commenting principles

Comments should be short, precise, grammatically clear, relevant, easy for another developer to
understand, and updated when the implementation changes.

Avoid long paragraphs inside the implementation unless the reasoning is genuinely complex.

### Good

```python
# Use task_id instead of session_id because recurring executions
# may create different session IDs for each run.
message_group_id = f"task:{task.id}"
```

### Avoid

```python
# Here we are setting the message group ID.
# The message group ID is important because SQS uses it
# to determine which messages belong to the same group.
# We set the message group ID to the task ID here.
message_group_id = f"task:{task.id}"
```

The second comment explains obvious implementation details instead of the important design
decision.

---

## 8. Docstrings

Use docstrings for public classes, methods, and interfaces where their behavior is not
self-evident.

### Good

```python
class TaskStore(ABC):
    """Persistence interface for scheduled tasks."""

    @abstractmethod
    def get_task(self, task_id: str) -> Task | None:
        """Return the task with the given ID, or None if it does not exist."""
        raise NotImplementedError
```

Docstrings should explain purpose, important behavior, constraints, side effects, and important
exceptions, when useful.

Do not write docstrings that simply repeat the method name.

---

## 9. Error Handling

Handle errors at the appropriate abstraction level.

Avoid:

```python
try:
    ...
except Exception:
    pass
```

Never silently swallow unexpected errors.

### Prefer specific exceptions

```python
try:
    task = self.store.get_task(task_id)
except TaskStoreError as exc:
    self.logger.error("Failed to retrieve task %s", task_id)
    raise TaskRetrievalError(task_id) from exc
```

Preserve the original exception using `raise NewError(...) from exc`.

Do not catch an exception unless you can meaningfully handle it, transform it, add useful context,
or recover from it.

---

## 10. Avoid Deep Nesting

Avoid deeply nested control flow.

### Avoid

```python
if task:
    if task.enabled:
        if task.schedule:
            if task.schedule.is_valid():
                ...
```

Prefer early returns:

```python
if task is None:
    return

if not task.enabled:
    return

if not task.schedule:
    return

if not task.schedule.is_valid():
    return

...
```

This keeps the main logic easier to follow.

---

## 11. Avoid Magic Values

Avoid unexplained literals.

### Avoid

```python
if retry_count > 3:
    ...
```

Prefer:

```python
MAX_RETRIES = 3

if retry_count > MAX_RETRIES:
    ...
```

For domain-specific values, prefer configuration or enums where appropriate.

---

## 12. Prefer Explicit Code

Python allows highly compact expressions, but readability should come first.

Avoid clever code that requires significant effort to understand.

### Prefer

```python
active_tasks = [
    task for task in tasks
    if task.enabled
]
```

over unnecessarily complex expressions. The best Python code is not necessarily the shortest code.

---

## 13. Avoid Unnecessary Duplication

If the same business logic appears multiple times, identify whether it belongs in a class method,
a shared abstraction, a reusable private method, or a domain object.

However, do not prematurely abstract code merely because two pieces currently look similar. The
abstraction should represent a genuine shared concept.

---

## 14. Keep Business Logic Separate From Infrastructure

Business logic should not unnecessarily depend directly on AWS SDKs, database clients, HTTP
frameworks, queue clients, file systems, or external APIs.

Prefer an abstraction:

```python
class TaskStore(ABC):

    @abstractmethod
    def save(self, task: Task) -> None:
        ...
```

Then infrastructure implementations can provide:

```python
class DynamoTaskStore(TaskStore):
    ...
```

This makes business logic easier to test and change.

---

## 15. Prefer Immutable Data Where Appropriate

Avoid unnecessary mutation. Prefer creating a new value when mutation does not provide a clear
benefit.

For data models, consider immutable/frozen models where appropriate.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    schedule: str
```

Immutability can make state transitions easier to reason about.

---

## 16. Avoid Global Mutable State

Avoid global objects that can be modified from anywhere.

### Avoid

```python
tasks = {}

def add_task(task):
    tasks[task.id] = task
```

Prefer encapsulating state:

```python
class TaskStore:
    def __init__(self):
        self._tasks = {}

    def add(self, task):
        self._tasks[task.id] = task
```

---

## 17. Logging

Logs should provide useful operational information.

Prefer:

```python
logger.info("Scheduled task %s for %s", task.id, next_run)
```

Avoid:

```python
logger.info("Something happened")
```

Do not log passwords, API keys, tokens, sensitive user data, or entire large request/response
objects unnecessarily.

Use structured and contextual logging where supported. See `ak-dev-code-quality` for this repo's
logger hierarchy (`logging.getLogger("ak.<module>")`) and log-level conventions.

---

## 18. Testing

New behavior should normally have tests. Tests should focus on behavior rather than implementation
details.

Prefer:

```python
def test_scheduler_returns_due_tasks():
    ...
```

over tests that depend heavily on private implementation details.

Test happy paths, error cases, boundary conditions, important state transitions, and integration
points where appropriate. Keep unit tests deterministic and independent.

See `ak-dev-testing-conventions` for this repo's pytest patterns, async testing, and mocking
conventions.

---

## 19. Avoid Overengineering

Do not introduce abstractions simply because they are technically possible.

Before adding a new class, interface, factory, strategy, manager, or helper, ask:

> "What concrete problem does this abstraction solve?"

Use the simplest design that preserves maintainability and future extensibility.

---

## 20. Backward Compatibility

When modifying existing code:

1. Understand the current behavior.
2. Identify existing callers.
3. Check existing tests.
4. Preserve behavior unless the requirement explicitly changes it.
5. Avoid unnecessary API changes.
6. Update tests when behavior intentionally changes.

Do not refactor unrelated code while implementing a focused feature unless the refactoring is
necessary.

---

## 21. Code Review Checklist

Before considering Python code complete, verify:

### Design

* [ ] Does each class have a clear responsibility?
* [ ] Does functionality belong inside a class?
* [ ] Should any behavior be an abstract method?
* [ ] Are dependencies injected where appropriate?
* [ ] Is business logic separated from infrastructure?

### Readability

* [ ] Are names clear?
* [ ] Are methods reasonably small?
* [ ] Is the control flow easy to follow?
* [ ] Is unnecessary nesting avoided?
* [ ] Is clever/overly compact code avoided?

### Comments

* [ ] Do comments explain **why** rather than **what**?
* [ ] Are comments short and clear?
* [ ] Are important design decisions documented?
* [ ] Are outdated comments removed?

### Error Handling

* [ ] Are exceptions specific?
* [ ] Are unexpected errors propagated?
* [ ] Is useful context added to errors?
* [ ] Are errors ever silently swallowed?

### Testing

* [ ] Is new behavior tested?
* [ ] Are edge cases covered?
* [ ] Are error paths tested?
* [ ] Are tests focused on behavior?

### Maintainability

* [ ] Is duplication minimized?
* [ ] Are magic values avoided?
* [ ] Is global mutable state avoided?
* [ ] Is the implementation simpler than necessary alternatives?

---

# Default Decision Rules

When uncertain, follow these rules:

1. Prefer a class method when behavior belongs to an object.
2. Use an abstract method when implementations need to provide different behavior.
3. Use standalone functions for genuinely independent, stateless utilities.
4. Prefer composition and dependency injection over hidden dependencies.
5. Keep methods small and focused.
6. Write comments for reasoning and design decisions, not obvious code.
7. Use type hints to make interfaces clear.
8. Raise meaningful exceptions instead of silently ignoring failures.
9. Prefer readable code over clever code.
10. Do not introduce abstractions without a concrete reason.
11. Keep business logic independent from infrastructure where practical.
12. Test behavior, especially new and changed behavior.
13. Preserve existing behavior unless the requirement explicitly changes it.
14. Optimize for the next developer who has to understand the code.
