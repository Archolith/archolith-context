"""Edge type constants for session graph relationships."""

# Session → File
TOUCHES = "TOUCHES"
MODIFIES = "MODIFIES"

# File → File
IMPORTS = "IMPORTS"

# Fact → Fact
CAUSED_BY = "CAUSED_BY"
SUPERSEDES = "SUPERSEDES"

# Fact → Decision
SUPPORTS = "SUPPORTS"

# * → Session
BELONGS_TO = "BELONGS_TO"

# Neo4j label for session isolation
CONTEXT_SESSION_LABEL = "ContextSession"
