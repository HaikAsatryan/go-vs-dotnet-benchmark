"""bench/validation; the equivalence suite (the CO / logging-sink /
connection-sweep modules were descoped at scope freeze and deleted).

Sibling package of `ledgerbench` in the same uv project. Pure-logic
modules (normalize.py, golden self-consistency) are importable with no Docker
and no running services; the live equivalence tests skip cleanly when their
env vars (LEDGER_GO_URL / LEDGER_DOTNET_URL) are absent.
"""
