# Role and authority

This chapter states what the handbook is for and which document owns what: the constitution, the architecture map, the design semantics and the reviewer checklists each keep their own authority, and this book carries only imperative rules for changing the body. It also names the domain manifest that owns module-to-domain assignment and the generated map read beside it, because moving code across a domain boundary is the owner's call rather than a manifest edit.

The handbook's own shape is fixed by its entrypoint (imperative rules per change
class, each naming its enforcing surface or stating that none does); the other
authorities are these. `BIBLE.md` owns constitutional
principles; `docs/ARCHITECTURE.md` owns the current structure, data flow, and
rationale map; `docs/DESIGN.md` owns visual and interaction semantics;
`docs/CHECKLISTS.md` owns reviewer items, severity, and output contracts. This
file does not duplicate their inventories or serve as a changelog.

One more owner to know before moving code: `ouroboros/domains.toml` is the SSOT
of the module-to-domain assignment (1:1 and complete over the tracked runtime
population) and pins the factual cross-domain dependency data as the baseline.
`docs/DOMAIN_MAP.md` is generated from it — read the map, edit the manifest,
then regenerate both with `python scripts/check_domains.py --write`
(`tests/test_domain_manifest.py` makes staleness red, and a new module with no
row is red too). A new cross-domain import direction, a wider cycle group, or a
cross-domain literal copy is a red gate, not a warning: needing one is an owner
decision, not a manifest edit. The witness-level detail behind the baseline —
every module-edge witness, the lazy/guarded/dynamic classification, the cycle
groups — is available from `python scripts/domain_report.py` on stdout, or
with `--output <path>` for an explicit report file. `docs/DOMAIN_MAP.md` remains
the maintained generated map.

Rules here describe current practice or a deliberately enforced standard. When
code and prose disagree, inspect the implementation and history, repair the
authoritative surfaces together, and retain the failure a non-obvious rule
prevents.

---

