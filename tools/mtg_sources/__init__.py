"""Per-host meta-source parsers for `tools/mtg fetch-meta`.

Spec named this package `tools/mtg/sources/`, but `tools/mtg` is already a
bash wrapper file (`exec python3 tools/mtg.py "$@"`) that the CLAUDE.md
quick-start, every doc, and every existing deck script invokes as
`tools/mtg <subcmd>`. A directory at that path would shadow the wrapper
and break the published UX. Renaming the wrapper is the wrong fix — it's
the documented entry point. So we deviate by one path segment: package
lives at `tools/mtg_sources/`. Same SSOT rules, same parser convention,
same registry contract.
"""
