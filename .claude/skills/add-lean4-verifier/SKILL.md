---
name: add-lean4-verifier
description: Add Lean4 verifier scaffold (config + env contract) for future formal checks.
---

# Add Lean4 Verifier (Scaffold)

This skill adds only the minimum scaffold:
1. Lean4 verifier config file.
2. Environment variable contract in `.env.example`.

No runtime behavior is changed in this skill.

## Apply

```bash
python scripts/vfriday_skill_apply.py .claude/skills/add-lean4-verifier
```

