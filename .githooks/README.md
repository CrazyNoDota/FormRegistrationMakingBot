# Git hooks

Secret-scanning hooks for this repo, powered by [gitleaks](https://github.com/gitleaks/gitleaks).

## Enable after cloning

These hooks are versioned but Git won't use them until you point Git at this folder:

```bash
git config core.hooksPath .githooks
```

You also need the `gitleaks` binary on your `PATH`:

```bash
# Linux x64 example
VER=8.30.1
curl -fsSL -o /tmp/gl.tgz \
  "https://github.com/gitleaks/gitleaks/releases/download/v${VER}/gitleaks_${VER}_linux_x64.tar.gz"
tar -xzf /tmp/gl.tgz -C ~/.local/bin gitleaks && chmod +x ~/.local/bin/gitleaks
```

(macOS: `brew install gitleaks`.)

## What it does

`pre-commit` runs `gitleaks protect --staged` and **blocks the commit** if a secret
(API key, token, etc.) is found in your staged changes.

- False positive? Add `# gitleaks:allow` on that line.
- Need to bypass once: `git commit --no-verify`.

Never commit real secrets — keep them in a local `.env` (which is git-ignored).
