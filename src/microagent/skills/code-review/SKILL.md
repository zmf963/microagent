---
name: code-review
description: Review code changes for bugs, security, and style.
triggers:
  - review
  - code review
  - check this code
  - audit
---

# Code Review

You are reviewing code changes. Be systematic.

## Procedure

1. Read every changed file with `read_file`.
2. Check for: bugs, security vulnerabilities, race conditions, missing error handling, unclear naming.
3. Use `grep` to find usages of changed functions — ensure all call sites still work.
4. Write findings as a structured report:
   - 🔴 Must fix (bugs, security)
   - 🟡 Should fix (style, clarity)
   - 🔵 Consider (optimizations, refactors)
5. If something looks wrong but you're not sure, flag it as "needs investigation" rather than guessing.

## Verification

- Every finding references a specific file and line number.
- No findings are based on assumptions — trace the actual code.
