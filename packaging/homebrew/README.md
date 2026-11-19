---
doc_id: fusion-agent-studio/packaging/homebrew
product: fusion-agent-studio
class: platform
entry: developer
doc_type: guide
title: Homebrew packaging
audience: maintainers
status: stable
version: 0.18.9
updated: 2026-11-19
owner: "@dahai"
source_of_truth:
  - packaging/homebrew/README.md
---

# Homebrew packaging

`fusion-agent-studio.rb` installs the PyPI package into a Homebrew-managed
virtualenv — all dependencies are declared as pinned, checksummed resources,
so installs are reproducible and need no network beyond the declared URLs.

Homebrew's `virtualenv_install_with_resources` only auto-detects
`py3-none-any` wheel resources (`language/python.rb` matches
`[.-]py3[^-]*-none-any.whl$`); platform-specific wheels (cp312 / abi3) get
unpacked and handed to pip as a directory, which fails. The four such
dependencies (`setproctitle`, `pydantic_core`, `pyyaml`, `psutil`) are
therefore excluded via `without:` and pip-installed from the fetched wheel
file inside a `res.stage` block (fetch + sha256 verification still happen;
the wheel is copied to its clean PyPI basename because pip rejects
Homebrew's hash-prefixed cache filename).

## Install

From the `dahai80/fusion-mlx` tap (same tap that carries `fusion-code`;
recommended — copy the formula into that repo as `Formula/fusion-agent-studio.rb`):

```sh
brew tap dahai80/fusion-mlx https://github.com/dahai80/homebrew-fusion-mlx
brew install dahai80/fusion-mlx/fusion-agent-studio
```

Local verification without a tap (brew rejects subdirectory paths, so stage
the formula into a throwaway local tap):

```sh
brew tap-new dahai80/local
cp packaging/homebrew/fusion-agent-studio.rb \
  "$(brew --repository)/Library/Taps/dahai80/homebrew-local/Formula/"
brew install --build-from-source dahai80/local/fusion-agent-studio
```

After install: `fusion-agent` (task CLI) and `fusion-mcp` (stdio MCP server)
are on PATH. The daemon/service lifecycle still uses `start.sh` from a repo
checkout; the formula ships the Python service only (the React web UI lives
in fusion-web-shell).

## Regenerating on a version bump

The formula pins the exact PyPI release and its full dependency closure.
After each release:

1. Resolve the closure: `python3 -m venv /tmp/fs-brew-deps && /tmp/fs-brew-deps/bin/pip install fusion-agent-studio==<ver> && /tmp/fs-brew-deps/bin/pip freeze --all | grep -v '^pip==' > /tmp/fs-brew-deps.freeze`
2. Regenerate the formula: `python3 packaging/homebrew/gen_formula.py <ver>`
   (PyPI JSON API → url + sha256; native wheels for
   `pydantic_core`/`pyyaml`/`psutil` are split `on_arm`/`on_intel` at
   `cp312`, `setproctitle` uses the `universal2` wheel, everything else uses
   the `py3-none-any` wheel)
3. Verify: `python3 packaging/homebrew/check_formula.py` — fails when the
   formula sdist version != pyproject version or any url/sha256 disagrees
   with the PyPI JSON API

CI runs `check_formula.py` on every push/PR, so an unreleased formula (the
0.18.5–0.18.9 drift this replaced) fails the build instead of passing
silently.
