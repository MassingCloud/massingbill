"""The core's own suite, which travels with the core.

Deliberately pytest-free: plain ``assert`` and a ``__main__`` runner in each
module, standard library only, so it runs unchanged in a consumer's harness
whatever that harness is. See ``docs/vendorable-core.md``.
"""
