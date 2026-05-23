"""Turn intent analyzer — rules-based classifier for driving fact selection.

Classifies the current user message to determine what knowledge domains
the model needs. No LLM call — pure heuristics + regex, runs in <50ms.

The intent drives the assembler's fact selection: instead of "top N facts
by generic score," it pulls "facts that serve this specific intent."
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class QuestionType(str, Enum):
    RECALL = "recall"          # "what did we decide about X?"
    ACTION = "action"          # "refactor the auth module"
    EXPLORATION = "exploration" # "how does the build pipeline work?"
    FOLLOWUP = "followup"      # continuation of previous topic
    DEBUG = "debug"            # "why is X failing?"
    STATUS = "status"          # "what have we done so far?"


class KnowledgeDomain(str, Enum):
    FILES = "files"
    DECISIONS = "decisions"
    ERRORS = "errors"
    ARCHITECTURE = "architecture"
    STATE = "state"
    TOOLS = "tools"
    GOALS = "goals"


@dataclass
class TurnIntent:
    question_type: QuestionType
    domains: list[KnowledgeDomain]
    explicit_refs: list[str]  # file paths, function names, identifiers
    is_topic_shift: bool
    goal_aligned: bool
    confidence: float  # 0-1, how confident the classifier is

    # Per-domain boost weights — facts matching these domains get scored higher
    domain_weights: dict[str, float] = field(default_factory=dict)


# Patterns for detecting explicit references
_FILE_PATH_RE = re.compile(
    r'(?:^|[\s`"\'])([a-zA-Z_][\w./\\-]*\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|md|sql|sh|css|html|rs|go|java|cpp|c|h))\b'
)
_FUNC_REF_RE = re.compile(
    r'(?:^|[\s`])([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\s*\(',
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(
    r'`([a-zA-Z_][\w.]*)`'
)

# Question type detection patterns
_RECALL_PATTERNS = [
    re.compile(r'\bwhat did (?:we|you|i)\b.*\b(?:decide|choose|agree|set|use)\b', re.I),
    re.compile(r'\bwhat (?:was|were|is) (?:the|our)\b.*\b(?:decision|approach|choice|plan)\b', re.I),
    re.compile(r'\bremember when\b', re.I),
    re.compile(r'\bearlier (?:we|you|i)\b', re.I),
    re.compile(r'\bwhat happened (?:with|to|when)\b', re.I),
    re.compile(r'\brecap\b|\bsummari[sz]e\b|\bwhat.s the status\b', re.I),
]

_DEBUG_PATTERNS = [
    re.compile(r'\bwhy (?:is|does|did|are|was)\b.*\b(?:fail|error|crash|break|wrong|bug)\b', re.I),
    re.compile(r'\b(?:error|exception|traceback|stack trace|failed|failing|broken)\b', re.I),
    re.compile(r'\bfix\b.*\b(?:bug|issue|error|problem)\b', re.I),
    re.compile(r'\bdebug\b', re.I),
    re.compile(r'\bnot working\b|\bdoesn.t work\b|\bwon.t\b.*\bwork\b', re.I),
]

_STATUS_PATTERNS = [
    re.compile(r'\bwhat have (?:we|you|i) done\b', re.I),
    re.compile(r'\bwhere (?:are|were) we\b', re.I),
    re.compile(r'\bprogress\b|\bstatus\b|\bso far\b', re.I),
    re.compile(r'\bwhat.s (?:left|remaining|next)\b', re.I),
]

_EXPLORATION_PATTERNS = [
    re.compile(r'\bhow does\b.*\bwork\b', re.I),
    re.compile(r'\bexplain\b|\bdescribe\b|\bwalk me through\b', re.I),
    re.compile(r'\bwhat is\b.*\b(?:for|used for|doing)\b', re.I),
    re.compile(r'\barchitecture\b|\bdesign\b|\bstructure\b', re.I),
]

_ACTION_PATTERNS = [
    re.compile(r'\b(?:refactor|rewrite|implement|add|create|build|write|update|change|modify|move|rename|delete|remove)\b', re.I),
    re.compile(r'\blet.s\b|\bgo ahead\b|\bplease\b.*\b(?:make|do|fix|add|update)\b', re.I),
]

# Domain detection patterns
_ERROR_DOMAIN_RE = re.compile(
    r'\b(?:error|exception|traceback|fail|crash|bug|issue|broken|TypeError|ValueError|KeyError|ImportError|SyntaxError|RuntimeError)\b',
    re.I,
)
_DECISION_DOMAIN_RE = re.compile(
    r'\b(?:decide|decision|chose|choice|approach|strategy|tradeoff|trade-off|rationale|why did we)\b',
    re.I,
)
_ARCHITECTURE_DOMAIN_RE = re.compile(
    r'\b(?:architecture|design|pattern|module|component|layer|service|pipeline|flow|data model|schema)\b',
    re.I,
)
_GOAL_DOMAIN_RE = re.compile(
    r'\b(?:goal|objective|task|milestone|requirement|spec|target)\b',
    re.I,
)
_TOOL_DOMAIN_RE = re.compile(
    r'\b(?:tool|command|script|npm|pip|cargo|make|docker|git|curl|test)\b',
    re.I,
)


def classify_intent(
    user_message: str,
    session_goal: str | None = None,
    recent_messages: list[dict] | None = None,
) -> TurnIntent:
    """Classify the current turn's intent for fact selection.

    Pure heuristics — no LLM call. Runs in <1ms typically.

    Args:
        user_message: The current user message text.
        session_goal: The session's overall goal (if known).
        recent_messages: Last 2-3 message pairs for topic-shift detection.

    Returns:
        TurnIntent with domains, references, question type, and weights.
    """
    if not user_message:
        return TurnIntent(
            question_type=QuestionType.FOLLOWUP,
            domains=[],
            explicit_refs=[],
            is_topic_shift=False,
            goal_aligned=True,
            confidence=0.1,
        )

    msg = user_message.strip()

    # Extract explicit references
    explicit_refs = _extract_references(msg)

    # Classify question type
    question_type = _classify_question_type(msg)

    # Detect knowledge domains
    domains = _detect_domains(msg, explicit_refs)

    # Topic shift detection
    is_topic_shift = _detect_topic_shift(msg, recent_messages)

    # Goal alignment
    goal_aligned = _check_goal_alignment(msg, session_goal)

    # Build domain weights based on question type and domains
    domain_weights = _build_domain_weights(question_type, domains, explicit_refs)

    # Confidence: higher when we found explicit signals
    confidence = 0.5
    if explicit_refs:
        confidence += 0.2
    if question_type != QuestionType.FOLLOWUP:
        confidence += 0.15
    if domains:
        confidence += 0.15
    confidence = min(confidence, 1.0)

    return TurnIntent(
        question_type=question_type,
        domains=domains,
        explicit_refs=explicit_refs,
        is_topic_shift=is_topic_shift,
        goal_aligned=goal_aligned,
        confidence=confidence,
        domain_weights=domain_weights,
    )


def _extract_references(msg: str) -> list[str]:
    """Extract file paths, function names, and backtick-quoted identifiers."""
    refs = set()

    for m in _FILE_PATH_RE.finditer(msg):
        refs.add(m.group(1))

    for m in _FUNC_REF_RE.finditer(msg):
        refs.add(m.group(1))

    for m in _IDENTIFIER_RE.finditer(msg):
        refs.add(m.group(1))

    return list(refs)


def _classify_question_type(msg: str) -> QuestionType:
    """Classify the message into a question type."""
    # Check patterns in priority order
    for pattern in _DEBUG_PATTERNS:
        if pattern.search(msg):
            return QuestionType.DEBUG

    for pattern in _RECALL_PATTERNS:
        if pattern.search(msg):
            return QuestionType.RECALL

    for pattern in _STATUS_PATTERNS:
        if pattern.search(msg):
            return QuestionType.STATUS

    for pattern in _EXPLORATION_PATTERNS:
        if pattern.search(msg):
            return QuestionType.EXPLORATION

    for pattern in _ACTION_PATTERNS:
        if pattern.search(msg):
            return QuestionType.ACTION

    return QuestionType.FOLLOWUP


def _detect_domains(msg: str, refs: list[str]) -> list[KnowledgeDomain]:
    """Detect which knowledge domains this message needs."""
    domains = []

    if refs:
        domains.append(KnowledgeDomain.FILES)

    if _ERROR_DOMAIN_RE.search(msg):
        domains.append(KnowledgeDomain.ERRORS)

    if _DECISION_DOMAIN_RE.search(msg):
        domains.append(KnowledgeDomain.DECISIONS)

    if _ARCHITECTURE_DOMAIN_RE.search(msg):
        domains.append(KnowledgeDomain.ARCHITECTURE)

    if _GOAL_DOMAIN_RE.search(msg):
        domains.append(KnowledgeDomain.GOALS)

    if _TOOL_DOMAIN_RE.search(msg):
        domains.append(KnowledgeDomain.TOOLS)

    return domains


def _detect_topic_shift(
    msg: str,
    recent_messages: list[dict] | None,
) -> bool:
    """Detect whether this message shifts topic from the recent conversation."""
    if not recent_messages:
        return False

    # Look for explicit shift markers
    shift_markers = re.compile(
        r'\b(?:actually|instead|switching|different|new topic|moving on|let.s talk about|forget about|never mind)\b',
        re.I,
    )
    if shift_markers.search(msg):
        return True

    # Compare word overlap with last user message
    last_user_msg = None
    for m in reversed(recent_messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            last_user_msg = content
            break

    if last_user_msg:
        current_words = set(re.findall(r'\b\w{4,}\b', msg.lower()))
        previous_words = set(re.findall(r'\b\w{4,}\b', last_user_msg.lower()))
        if current_words and previous_words:
            overlap = len(current_words & previous_words) / max(len(current_words), 1)
            if overlap < 0.1:
                return True

    return False


def _check_goal_alignment(msg: str, session_goal: str | None) -> bool:
    """Check whether this message aligns with the session goal."""
    if not session_goal:
        return True  # No goal = assume aligned

    goal_words = set(re.findall(r'\b\w{4,}\b', session_goal.lower()))
    msg_words = set(re.findall(r'\b\w{4,}\b', msg.lower()))

    if not goal_words:
        return True

    overlap = len(goal_words & msg_words) / max(len(goal_words), 1)
    return overlap > 0.05


def _build_domain_weights(
    question_type: QuestionType,
    domains: list[KnowledgeDomain],
    explicit_refs: list[str],
) -> dict[str, float]:
    """Build per-domain boost weights for fact scoring.

    Facts whose type matches a high-weight domain get boosted in scoring.
    The weight maps domain -> fact_type relationships.
    """
    weights: dict[str, float] = {}

    # Base: all detected domains get a boost
    for d in domains:
        weights[d.value] = 0.3

    # Question type boosts
    if question_type == QuestionType.DEBUG:
        weights["errors"] = max(weights.get("errors", 0), 0.6)
        weights["state"] = max(weights.get("state", 0), 0.4)
        weights["files"] = max(weights.get("files", 0), 0.3)
    elif question_type == QuestionType.RECALL:
        weights["decisions"] = max(weights.get("decisions", 0), 0.5)
        weights["goals"] = max(weights.get("goals", 0), 0.4)
        weights["state"] = max(weights.get("state", 0), 0.3)
    elif question_type == QuestionType.STATUS:
        weights["goals"] = max(weights.get("goals", 0), 0.5)
        weights["decisions"] = max(weights.get("decisions", 0), 0.4)
        weights["files"] = max(weights.get("files", 0), 0.3)
        weights["state"] = max(weights.get("state", 0), 0.3)
    elif question_type == QuestionType.EXPLORATION:
        weights["architecture"] = max(weights.get("architecture", 0), 0.5)
        weights["state"] = max(weights.get("state", 0), 0.3)
    elif question_type == QuestionType.ACTION:
        weights["files"] = max(weights.get("files", 0), 0.4)
        weights["state"] = max(weights.get("state", 0), 0.3)
        weights["decisions"] = max(weights.get("decisions", 0), 0.2)

    # Explicit file refs → boost files domain hard
    if explicit_refs:
        weights["files"] = max(weights.get("files", 0), 0.5)

    return weights


# Mapping from KnowledgeDomain to FactType values for filtering
DOMAIN_TO_FACT_TYPES: dict[str, list[str]] = {
    "files": ["file_state"],
    "errors": ["error"],
    "decisions": ["decision"],
    "architecture": ["state", "observation"],
    "state": ["state", "goal"],
    "tools": ["tool_result"],
    "goals": ["goal"],
}
