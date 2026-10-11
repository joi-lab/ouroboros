"""CyberGym benchmark adapter package.

The package contains operator-side protocol, ledger, and sidecar helpers.  The
optional upstream ``cybergym`` package and Docker integration are imported by
the launcher only after an admitted run explicitly asks for them; importing
this package itself never contacts Docker, a provider, or the benchmark.
"""
