---
name: debugging
description: Systematic debugging: reproduce → isolate → fix → verify.
triggers:
  - debug
  - bug
  - error
  - crash
  - not working
  - diagnose
  - traceback
---

# Systematic Debugging

Don't guess — trace the evidence.

## 4-Phase Process

### 1. Reproduce
- Run the failing command with `bash` to confirm the error.
- Capture the exact error message, exit code, and stack trace.
- If you can't reproduce it, note what you tried.

### 2. Isolate
- Use `read_file` to read the file at the top of the stack trace.
- Use `grep` to find all references to the failing function.
- Trace the data flow: where does the bad value originate?
- Add `bash` debug prints if needed (remove them after).

### 3. Fix
- Fix the root cause, not the symptom.
- Check sibling call paths for the same bug class.
- Use `edit_file` for targeted changes.

### 4. Verify
- Run the original failing command again — it must pass.
- Run the full test suite — no regressions.
- If there are no tests, write one with `write_file`.
