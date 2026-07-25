---
name: tdd
description: Test-driven development: red → green → refactor.
triggers:
  - tdd
  - test-driven
  - add tests
  - write tests first
---

# Test-Driven Development

Follow the Red-Green-Refactor cycle strictly.

## Cycle

1. **Red** — Write a failing test that defines the desired behavior.
   - Use `write_file` to create the test.
   - Run with `bash` to confirm it FAILS.
2. **Green** — Write the minimum code to make the test pass.
   - Use `write_file` or `edit_file` to implement.
   - Run tests again — they must PASS.
3. **Refactor** — Clean up without changing behavior.
   - Tests must stay green after refactoring.

## Rules

- Never write implementation before the test.
- One test at a time — don't batch unrelated tests.
- Tests must assert actual behavior, not implementation details.
- Run `bash` to verify each step — don't assume tests pass.
