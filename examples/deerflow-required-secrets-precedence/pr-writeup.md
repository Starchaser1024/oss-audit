Fixes #7568

## Why

`parse_required_secrets` collapses duplicate `required-secrets` declarations by name, keeping the first entry and skipping later ones, so declaration order decides whether a secret is required. With `- name: ERP_TOKEN / optional: true` followed by a bare `- ERP_TOKEN`, the skill ends up with `SecretRequirement(name='ERP_TOKEN', optional=True)`, and `skill_activation_middleware.py:747` only records a missing secret under `elif not req.optional`. Reversing the two lines yields `optional=False`.

`export.py::_manifest` carried an identical first-wins `seen` set for the same field, causing exported manifests to disagree with author intent as well.

## What changed

Duplicates still collapse into a single entry at the first position, but the entry becomes required whenever any declaration of that name is required. `parser.py` tracks each name's slot and upgrades the stored `SecretRequirement` when a subsequent required declaration appears; `export.py` applies the same fail-closed rule to exported manifests.

## Bug fix verification

- RED on unmodified `main` (`04dc9e533`): `1 failed, 1 passed, 151 deselected` (`SecretRequirement(name='ERP_TOKEN', optional=True) != SecretRequirement(name='ERP_TOKEN', optional=False)`).
- GREEN on fix (`82260e3ec`): `test_skill_request_scoped_secrets.py`, `test_skills_validation.py`, and `test_skill_export.py` — **199 passed**; adjacent activation suites — **92 passed**; Ruff check and format clean.
