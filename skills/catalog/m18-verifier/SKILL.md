---
name: m18-verifier
description: Internal platform acceptance skill. Invoke only during the M18 smoke test.
disable-model-invocation: true
---

# M18 verification

Use the `read` tool to read `/workspace/m11-marker.txt`. Reply with exactly
`M18_SKILL_OK:` followed by the complete file contents. Do not add any other text.
