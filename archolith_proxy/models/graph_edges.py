"""Edge type constants for session graph relationships."""

# Session → File
TOUCHES = "TOUCHES"
MODIFIES = "MODIFIES"

# Fact → Fact
SUPERSEDES = "SUPERSEDES"

# * → Session
BELONGS_TO = "BELONGS_TO"

# Neo4j label for session isolation
CONTEXT_SESSION_LABEL = "ContextSession"
