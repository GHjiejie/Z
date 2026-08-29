# Test and Verification

- Start with focused checks for the files and behavior changed.
- Run broader lint, type-check, build, or test commands when risk justifies them.
- Record the exact command, exit code, and relevant output for every claimed check.
- Distinguish passed, failed, skipped, unavailable, and not configured.
- Never claim that a command ran when it did not execute in the sandbox.
- Do not hide failing output; explain whether it is caused by the change or pre-existing.
