# Security Reviewer Agent

Commodity agent from the first published agent family (#570). Capability:
STRIDE pass over attack surfaces, authn/authz gaps, injection/SSRF/path-traversal, secret leakage.

## Capability summary

- Prompt pack: built-in (reviewer-security reviewer contract, blind-review by default)
- Tool allowlist: `artifact_get_source`, `artifact_list_all`
- KB binding: none (self-contained review contract)
- Model binding: replaceable — ships `local` (MLX local model) and `pro`
  (cloud model via gateway) SKUs. The binding is NOT part of the capability.

## SKU editions

| SKU | Model | Pitch |
|---|---|---|
| `local` | MLX local model (e.g. Qwen3.5-4B) | Zero data egress — artifact text never leaves the machine |
| `pro` | Cloud frontier model via gateway model matrix | Stronger recall on subtle violations |

## Docs pack (release gate G5)

- [README.md](README.md) / [README_CN.md](README_CN.md)
- [QUICKSTART.md](QUICKSTART.md)
- [CAPABILITY_SPEC.md](CAPABILITY_SPEC.md)
- [EVAL_REPORT.md](EVAL_REPORT.md)
- [CHANGELOG.md](CHANGELOG.md)

All five items are mandatory; commodity publish validation rejects listings
missing any of them.
