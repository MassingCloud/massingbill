"""Test package.

Present so ``conftest`` is importable as ``tests.conftest``. Without it, pytest
loads the file as a top-level ``conftest`` module and a class imported via
``tests.conftest`` is a *different* object -- which quietly breaks
``pytest.raises`` against exceptions defined there.
"""
