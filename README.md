# delay

## Known limitations

- **Pre-encryption plaintext is still retrievable from git history.** The
  encryption migration (`encrypted/` + `encryption/`) protects only the current
  and future HEAD. The business scripts committed before it (`baker/*.py`,
  `scripts/*.py`, `jp-relay/*`) can still be recovered in full, e.g.
  `git show <old-commit>:baker/delays.py`. Erasing them for good would require a
  history rewrite (rewrites every commit sha) or a fresh repository; neither has
  been done, so treat the history as still containing the plaintext.
