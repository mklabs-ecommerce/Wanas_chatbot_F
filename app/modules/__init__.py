"""Business modules.

Boundary rules (Section 3 of chatbot-build-from-zero.md), enforced by convention:
  * A module is reached only through its public ``service.py`` functions.
  * A module's ``repository.py`` is the only code that may query that module's own tables.
    No module touches another module's tables.
  * ``chat`` orchestrates the others; it owns no business logic and no data beyond
    conversation history.
"""
