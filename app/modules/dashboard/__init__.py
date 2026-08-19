"""dashboard module — public surface is service.py (see app/modules/__init__.py for the rules).

Unusual among the modules: it owns no tables and no business rules. It is a read-only
view that composes what the other modules already expose through their service.py, so
the store owner can see a conversation and everything it produced in one place.

Because it owns nothing, nothing may depend on it.
"""
