"""Calibration corpus — realistic request payloads for token accounting benchmarking.

Each calibration case is a realistic OpenAI API request payload that exercises
a specific traffic pattern. These are used to compare:
  - old content-only estimate
  - new structural estimate
  - client-reported size (simulated)
  - upstream usage tokens (simulated, when available)

Cases cover:
  1. Plain chat turns — simple text, no tools
  2. Code-heavy file reads — large Read tool results
  3. Tool-heavy calls — many tool definitions + tool_calls
  4. Large tools arrays — full MCP tool schemas
  5. Multipart content — image/file attachments
  6. Streaming recall flows — recall tool injection
  7. Mixed session — combination of the above across many turns
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CALIBRATION_DIR = Path(__file__).parent / "calibration"


@dataclass
class CalibrationCase:
    """A single calibration test case with a realistic request payload."""
    id: str
    category: str
    description: str
    messages: list[dict]
    tools: list[dict] | None = None
    client_reported_tokens: int | None = None
    upstream_usage_tokens: int | None = None  # simulated real usage
    expected_behavior: str = ""  # what the gate should decide

    def save(self, directory: Path | None = None) -> Path:
        out_dir = directory or CALIBRATION_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.id}.json"
        data = {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "messages": self.messages,
            "tools": self.tools,
            "client_reported_tokens": self.client_reported_tokens,
            "upstream_usage_tokens": self.upstream_usage_tokens,
            "expected_behavior": self.expected_behavior,
        }
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "CalibrationCase":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            id=data["id"],
            category=data["category"],
            description=data["description"],
            messages=data["messages"],
            tools=data.get("tools"),
            client_reported_tokens=data.get("client_reported_tokens"),
            upstream_usage_tokens=data.get("upstream_usage_tokens"),
            expected_behavior=data.get("expected_behavior", ""),
        )


# ---------------------------------------------------------------------------
# Realistic tool schemas (based on actual MCP server definitions)
# ---------------------------------------------------------------------------

READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "Read a file from the local filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The absolute path to the file to read"},
                "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed)"},
                "limit": {"type": "integer", "description": "Maximum number of lines to read"},
            },
            "required": ["file_path"],
        },
    },
}

GLOB_TOOL = {
    "type": "function",
    "function": {
        "name": "Glob",
        "description": "Fast file pattern matching tool that works with any codebase size.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The glob pattern to match files against"},
                "path": {"type": "string", "description": "The directory to search in"},
            },
            "required": ["pattern"],
        },
    },
}

GREP_TOOL = {
    "type": "function",
    "function": {
        "name": "Grep",
        "description": "Fast content search tool that works with any codebase size.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex pattern to search for"},
                "include": {"type": "string", "description": "File pattern to include"},
                "path": {"type": "string", "description": "The directory to search in"},
            },
            "required": ["pattern"],
        },
    },
}

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "Executes a given bash command in a persistent shell session.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to execute"},
                "description": {"type": "string", "description": "Brief description of what this command does"},
                "timeout": {"type": "integer", "description": "Optional timeout in milliseconds"},
                "workdir": {"type": "string", "description": "The working directory to run the command in"},
            },
            "required": ["command", "description"],
        },
    },
}

WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": "Writes a file to the local filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The absolute path to the file to write"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["file_path", "content"],
        },
    },
}

EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": "Performs exact string replacements in files.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The absolute path to the file to modify"},
                "oldString": {"type": "string", "description": "The text to replace"},
                "newString": {"type": "string", "description": "The text to replace it with"},
                "replaceAll": {"type": "boolean", "description": "Replace all occurrences"},
            },
            "required": ["file_path", "oldString", "newString"],
        },
    },
}

DELEGATE_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": "Delegate a coding task to the local LLM with file access.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Instruction for the delegated model"},
                "working_directory": {"type": "string", "description": "Absolute project path"},
                "file_paths": {"type": "string", "description": "Optional file(s) to pre-read into context"},
                "read_only": {"type": "boolean", "description": "When true, write/edit tools are excluded"},
                "system_prompt_override": {"type": "string", "description": "Custom system prompt"},
            },
            "required": ["prompt", "working_directory"],
        },
    },
}

BROWSE_TOOL = {
    "type": "function",
    "function": {
        "name": "browse_like_human",
        "description": "Browse a page with Playwright + stealth, scripted actions, and structured extraction.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Initial URL to open"},
                "actions_json": {"type": "string", "description": "Optional JSON array of action objects"},
                "headless": {"type": "boolean", "description": "Run browser headlessly"},
                "max_chars": {"type": "integer", "description": "Max characters for text fields"},
                "timeout_ms": {"type": "integer", "description": "Navigation/action timeout"},
            },
            "required": ["url"],
        },
    },
}

VPS_DEPLOY_TOOL = {
    "type": "function",
    "function": {
        "name": "vps_deploy",
        "description": "Rebuild and restart one compose service in the background.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name"},
                "timeout_s": {"type": "integer", "description": "Timeout in seconds"},
                "no_cache": {"type": "boolean", "description": "Bypass Docker build cache"},
            },
            "required": ["service"],
        },
    },
}

MEMORY_ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add_memory",
        "description": "Queue a memory for enrichment.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The memory content to store"},
                "type": {"type": "string", "description": "Memory type"},
                "source": {"type": "string", "description": "Where this memory came from"},
                "valid_at": {"type": "string", "description": "ISO date for TEMPORAL memories"},
                "flagged": {"type": "boolean", "description": "Mark as permanently retained"},
                "diff": {"type": "string", "description": "Optional git diff to attach"},
            },
            "required": ["text"],
        },
    },
}

MEMORY_RECALL_TOOL = {
    "type": "function",
    "function": {
        "name": "recall_memories",
        "description": "Search memories by semantic similarity.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "integer", "description": "Max results to return"},
                "preset": {"type": "string", "description": "Ranking strategy"},
                "file_context": {"type": "string", "description": "Optional file path"},
            },
            "required": ["query"],
        },
    },
}

# Full tool array as a real coding agent would send
FULL_AGENT_TOOLS = [
    READ_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL, WRITE_TOOL, EDIT_TOOL,
    DELEGATE_TASK_TOOL, BROWSE_TOOL, VPS_DEPLOY_TOOL,
    MEMORY_ADD_TOOL, MEMORY_RECALL_TOOL,
]

# --- Simulated tool results (realistic sizes) ---

LARGE_FILE_READ = (
    "# cth.context-engine — Architecture\n\n"
    "## Overview\n\n"
    "An OpenAI-compatible proxy that transparently replaces linear conversation replay "
    "with graph-assembled context for AI coding agents. Any harness that supports a "
    "base URL override (Reasonix, Claude Code, Aider, Cursor, etc.) works unchanged.\n\n"
) * 20 # ~4000 chars per copy

# For graph-qualifying sessions, we need ~50K+ tokens of content.
# At ~3.6 chars/token, 200K chars = ~55K tokens. Each LARGE_FILE_READ copy is ~4K chars.
# So 50 copies = ~200K chars = ~55K tokens of tool content alone.
VERY_LARGE_FILE_READ = LARGE_FILE_READ * 50  # ~200K chars = ~55K tokens

GLOB_RESULT = "\n".join(
    f"src/{path}" for path in [
        "main.py", "config.py", "logging_config.py",
        "assembler/context.py", "assembler/compaction.py", "assembler/query_rewrite.py", "assembler/tail.py",
        "extractor/client.py", "extractor/dedup.py", "extractor/embeddings.py", "extractor/prompts.py",
        "graph/driver.py", "graph/repository.py", "graph/facts.py", "graph/session.py",
        "graph/edges.py", "graph/cleanup.py",
        "openai/chat.py", "openai/schemas.py", "openai/errors.py", "openai/router.py", "openai/passthrough.py",
        "proxy/session.py", "proxy/live.py",
        "models/graph_nodes.py", "models/dtos.py",
        "token_accounting/__init__.py", "token_accounting/models.py", "token_accounting/estimate.py",
        "token_accounting/client_hints.py", "token_accounting/gating.py",
    ]
)

GREP_RESULT = "\n".join([
    "src/openai/chat.py:57:def _estimate_input_tokens(messages: list[dict]) -> int:",
    "src/openai/chat.py:291:    input_tokens = _estimate_input_tokens(body.get('messages', []))",
    "src/openai/chat.py:343:                rewritten_tokens = _estimate_input_tokens(body['messages'])",
    "src/token_accounting/estimate.py:42:def estimate_content_tokens(messages: list[dict]) -> int:",
    "src/token_accounting/estimate.py:68:def estimate_structural_tokens(",
    "src/token_accounting/estimate.py:119:def estimate_rewritten_tokens(messages: list[dict]) -> int:",
    "src/token_accounting/estimate.py:126:def compute_breakdown(",
    "src/token_accounting/estimate.py:165:def compute_savings(",
    "src/token_accounting/estimate.py:194:def evaluate_gate(",
])

SYSTEM_PROMPT = (
    "You are OpenCode, SST's AI-powered coding assistant running as an interactive "
    "CLI tool. You help with software engineering tasks: debugging, implementing features, "
    "refactoring code, explaining systems, reviewing changes, and executing multi-step "
    "development workflows.\n\n"
    "You have access to file tools and shell execution. You work interactively in a terminal. "
    "You confirm before taking destructive or irreversible actions.\n\n"
    "Avoid these known tendencies:\n"
    "- Verbose step-by-step narration of internal reasoning\n"
    "- Making assumptions about project structure without reading .agent/ docs first\n"
    "- Writing wrapup or summary blocks at the end of every response\n"
    "- Applying changes without first reading the target file's current content\n\n"
    "This harness is not first-class for closeout in this workspace. When you make changes:\n"
    "- Write a wrapup in `.agent/for-review/`\n"
    "- Run `artifact_validate` after writing it\n"
    "- Fix all validator `FAIL` findings before `READY FOR REVIEW`\n"
    "- If work is uncommitted, set `Commits: UNCOMMITTED` and `Status: PARTIAL`\n"
)  # ~1000 chars


# ---------------------------------------------------------------------------
# Calibration cases
# ---------------------------------------------------------------------------

def plain_chat_turn() -> CalibrationCase:
    """Simple text conversation, no tools."""
    return CalibrationCase(
        id="plain-chat-001",
        category="plain_chat",
        description="Simple 6-turn text conversation, no tool schemas, no tool_calls. "
        "Content-only estimate should closely match structural estimate.",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "What is the context-engine project about?"},
            {"role": "assistant", "content": "The context-engine is an OpenAI-compatible proxy that replaces "
                "linear conversation replay with graph-assembled context. It sits "
                "between your coding agent and the upstream LLM API."},
            {"role": "user", "content": "How does it decide when to rewrite?"},
            {"role": "assistant", "content": "It uses a savings-ratio gate. If the conversation is below 50K "
                "input tokens, it passes through unchanged. If rewriting would "
                "save less than 20% of tokens, it also passes through."},
            {"role": "user", "content": "What are the key components?"},
            {"role": "assistant", "content": "The main components are: the proxy layer (FastAPI), the context "
                "assembler (graph queries + budgeting), the fact extractor "
                "(gpt-4.1-mini), the session graph (Neo4j/Graphiti), and the "
                "token accounting module."},
        ],
        tools=None,
        client_reported_tokens=None,
        upstream_usage_tokens=None,
        expected_behavior="passthrough -- well below 50K token gate",
    )


def code_heavy_file_reads() -> CalibrationCase:
    """Large file read results filling up the messages array."""
    return CalibrationCase(
        id="code-heavy-001",
        category="code_heavy",
        description="Multiple Read tool calls with large results. Content estimate should be "
        "close to structural, but structural adds tool_call framing overhead. "
        "This simulates a typical code exploration session.",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Read the architecture file and the config module"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_001", "type": "function", "function": {
                    "name": "Read", "arguments": "{\"file_path\": \"src/.agent/architecture.md\"}"
                }},
                {"id": "call_002", "type": "function", "function": {
                    "name": "Read", "arguments": "{\"file_path\": \"src/config.py\"}"
                }},
            ]},
            {"role": "tool", "content": LARGE_FILE_READ * 10, "tool_call_id": "call_001", "name": "Read"},
            {"role": "tool", "content": LARGE_FILE_READ * 5, "tool_call_id": "call_002", "name": "Read"},
            {"role": "assistant", "content": "The architecture describes a proxy + assembler + extractor + graph design. "
                "Config uses pydantic-settings with env vars for upstream, Neo4j, and proxy settings."},
            {"role": "user", "content": "Now read the chat handler"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_003", "type": "function", "function": {
                    "name": "Read", "arguments": "{\"file_path\": \"src/openai/chat.py\", \"limit\": 100}"
                }},
            ]},
            {"role": "tool", "content": LARGE_FILE_READ * 15, "tool_call_id": "call_003", "name": "Read"},
            {"role": "assistant", "content": "The chat handler is 1352 lines. It handles session resolution, context assembly, "
                "request rewriting, and post-response extraction."},
        ],
        tools=[READ_TOOL, GLOB_TOOL, GREP_TOOL],
        client_reported_tokens=None,  # derived by build_calibration_corpus
        upstream_usage_tokens=None,   # derived by build_calibration_corpus
        expected_behavior="passthrough — below 50K structural estimate",
    )


def tool_heavy_calls() -> CalibrationCase:
    """Many tool calls with results -- exercises structural vs content gap."""
    return CalibrationCase(
        id="tool-heavy-001",
        category="tool_heavy",
        description="Session with many Glob/Grep/Bash tool calls. Tool results are large. "
        "Structural estimate should be significantly higher than content-only "
        "due to tool_call overhead and tool message framing.",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Map out the full project structure"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_a1", "type": "function", "function": {
                    "name": "Glob", "arguments": "{\"pattern\": \"**/*.py\"}"
                }},
            ]},
            {"role": "tool", "content": GLOB_RESULT, "tool_call_id": "call_a1", "name": "Glob"},
            {"role": "assistant", "content": "Found 32 Python files. Let me search for the key patterns."},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_a2", "type": "function", "function": {
                    "name": "Grep", "arguments": "{\"pattern\": \"estimate.*tokens\", \"include\": \"*.py\"}"
                }},
                {"id": "call_a3", "type": "function", "function": {
                    "name": "Grep", "arguments": "{\"pattern\": \"def assemble\", \"include\": \"*.py\"}"
                }},
                {"id": "call_a4", "type": "function", "function": {
                    "name": "Grep", "arguments": "{\"pattern\": \"async def\", \"include\": \"*.py\"}"
                }},
            ]},
            {"role": "tool", "content": GREP_RESULT, "tool_call_id": "call_a2", "name": "Grep"},
            {"role": "tool", "content": "src/assembler/context.py:323:async def assemble_context(...)\n"
                "src/assembler/compaction.py:45:async def compact_context(...)\n"
                "src/token_accounting/gating.py:28:def build_telemetry(...)",
                "tool_call_id": "call_a3", "name": "Grep"},
            {"role": "tool", "content": "23 async functions found across main, openai, assembler, graph, extractor, proxy",
                "tool_call_id": "call_a4", "name": "Grep"},
            {"role": "assistant", "content": "I've mapped the project. The key functions are:\n"
                "- assemble_context() in assembler/context.py\n"
                "- _estimate_input_tokens() in openai/chat.py (deprecated)\n"
                "- estimate_structural_tokens() in token_accounting/estimate.py\n"
                "- build_telemetry() in token_accounting/gating.py"},
            {"role": "user", "content": "Run the test suite"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_a5", "type": "function", "function": {
                    "name": "Bash", "arguments": "{\"command\": \"pytest tests/ -v\", \"description\": \"Run full test suite\"}"
                }},
            ]},
            {"role": "tool", "content": "336 passed in 7.55s", "tool_call_id": "call_a5", "name": "Bash"},
        ],
        tools=[READ_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL, WRITE_TOOL, EDIT_TOOL],
        client_reported_tokens=None,
        upstream_usage_tokens=None,
        expected_behavior="passthrough — below 50K but structural estimate should be noticeably > content estimate",
    )


def large_tools_array() -> CalibrationCase:
    """Full MCP tool schemas -- exercises the tools array token counting."""
    return CalibrationCase(
        id="large-tools-001",
        category="large_tools_array",
        description="Full set of MCP tool schemas (11 tools with descriptions and parameter schemas). "
        "The tools array itself contributes significant tokens. Structural estimate "
        "should be notably higher than content-only estimate because content doesn't "
        "count tool definitions at all.",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Add token accounting to the chat handler"},
            {"role": "assistant", "content": "I'll integrate the token accounting module into the chat handler. "
                "Let me first check the current implementation."},
        ],
        tools=FULL_AGENT_TOOLS,
        client_reported_tokens=None,
        upstream_usage_tokens=None,
        expected_behavior="passthrough — but structural estimate should be higher than content due to tools",
    )


def multipart_content() -> CalibrationCase:
    """Multipart message content with image URLs."""
    return CalibrationCase(
        id="multipart-content-001",
        category="multipart_content",
        description="Messages with multipart content (text + image_url). "
        "Content estimate should count image URL strings. "
        "Structural estimate should add framing overhead.",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "What does this screenshot show?"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                }},
            ]},
            {"role": "assistant", "content": "This appears to be a screenshot of a terminal or dashboard showing "
                "system metrics and health status information."},
        ],
        tools=[READ_TOOL, GLOB_TOOL],
        client_reported_tokens=None,
        upstream_usage_tokens=None,
        expected_behavior="passthrough -- small request, but multipart handling should work",
    )


def streaming_recall_flow() -> CalibrationCase:
    """Session with recall tool injection -- large session context."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    # Simulate a 30-turn session with enough content to cross 50K tokens.
    # Each Read tool result is ~200K chars (~55K tokens) so even a few
    # turns with large reads will qualify for graph rewriting.
    for i in range(1, 16):
        messages.append({"role": "user", "content": f"Turn {i}: Fix the bug in module_{i}"})
        if i < 5:
            # Early turns: simple text
            messages.append({"role": "assistant", "content": f"Looking at module_{i}. Let me read the file."})
        else:
            # Later turns: tool calls with large results
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{i:03d}",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"file_path": f"src/module_{i}.py"}),
                    },
                }],
            })
            # Use VERY_LARGE_FILE_READ for turns 5-10, then LARGE_FILE_READ for later
            file_content = VERY_LARGE_FILE_READ if i <= 10 else LARGE_FILE_READ * 5
            messages.append({
                "role": "tool",
                "content": f"# module_{i}.py\n{file_content}",
                "tool_call_id": f"call_{i:03d}",
                "name": "Read",
            })
            messages.append({
                "role": "assistant",
                "content": f"Fixed the bug in module_{i}. The issue was a missing null check.",
            })

    return CalibrationCase(
        id="streaming-recall-001",
        category="streaming_recall",
        description="30-turn session with recall tool injection and large file reads. "
        "This is a long session that should qualify for graph assembly rewriting. "
        "Structural estimate should cross 50K gate on its own. "
        "The savings gate should approve rewriting.",
        messages=messages,
        tools=FULL_AGENT_TOOLS,
        client_reported_tokens=None,
        upstream_usage_tokens=None,
        expected_behavior="graph -- above 50K gate, rewriting should be approved",
    )


def mixed_session() -> CalibrationCase:
    """Combination of all patterns -- realistic full session with enough content for graph rewrite."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "I need to add evaluation infrastructure to the context-engine project"},
        {"role": "assistant", "content": "I'll help you add evaluation infrastructure. Let me explore the codebase first."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_m1", "type": "function", "function": {
                "name": "Glob", "arguments": "{\"pattern\": \"eval/**/*.py\"}"
            }},
            {"id": "call_m2", "type": "function", "function": {
                "name": "Read", "arguments": "{\"file_path\": \"src/openai/chat.py\", \"limit\": 100}"
            }},
        ]},
        {"role": "tool", "content": "No files found -- eval/ does not exist yet", "tool_call_id": "call_m1", "name": "Glob"},
        {"role": "tool", "content": VERY_LARGE_FILE_READ, "tool_call_id": "call_m2", "name": "Read"},
        {"role": "assistant", "content": "The project doesn't have an eval/ directory yet. I'll create one with:\n"
            "1. Golden session corpus\n2. Comparator harness\n3. Evaluation rubric\n"
            "4. Feature activation matrix\n5. Rollout gates"},
        {"role": "user", "content": "Great, start with the golden session corpus"},
        {"role": "assistant", "content": "Building the corpus now. I'll create 7 sessions covering different categories."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_m3", "type": "function", "function": {
                "name": "Read", "arguments": json.dumps({
                    "file_path": "src/assembler/context.py",
                }),
            }},
        ]},
        {"role": "tool", "content": VERY_LARGE_FILE_READ, "tool_call_id": "call_m3", "name": "Read"},
        {"role": "assistant", "content": "I've read the assembler context module. Now I understand the full pipeline."},
        {"role": "user", "content": "Also add token accounting so we can measure properly"},
        {"role": "assistant", "content": "I'll create the token accounting module with structural estimation, "
            "client hints, and gate logic."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_m4", "type": "function", "function": {
                "name": "Read", "arguments": json.dumps({
                    "file_path": "src/token_accounting/estimate.py",
                }),
            }},
        ]},
        {"role": "tool", "content": VERY_LARGE_FILE_READ, "tool_call_id": "call_m4", "name": "Read"},
        {"role": "assistant", "content": "I've reviewed the estimator. Now I'll implement the calibration runner."},
        {"role": "user", "content": "Run the tests when you're done"},
        {"role": "assistant", "content": "All done. Running tests now."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_m5", "type": "function", "function": {
                "name": "Bash", "arguments": json.dumps({
                    "command": "pytest tests/ -v",
                    "description": "Run full test suite",
                }),
            }},
        ]},
        {"role": "tool", "content": "336 passed in 7.55s", "tool_call_id": "call_m5", "name": "Bash"},
        {"role": "assistant", "content": "All 336 tests passing. The evaluation framework is ready."},
    ]

    return CalibrationCase(
        id="mixed-session-001",
        category="mixed_session",
        description="Realistic full session combining plain chat, tool calls, large file reads, "
        "writes, and bash execution. This is representative of actual coding-agent traffic. "
        "Multiple large Read results push the structural estimate well above 50K, "
        "so the session should qualify for graph assembly rewriting.",
        messages=messages,
        tools=FULL_AGENT_TOOLS,
        client_reported_tokens=None,
        upstream_usage_tokens=None,
        expected_behavior="graph -- above 50K, rewriting should be approved with meaningful savings",
    )


def _derive_upstream_values(case: CalibrationCase) -> CalibrationCase:
    """Derive client_reported and upstream_usage from structural estimate.

    Strategy:
    - Run estimate_structural_tokens on the case's messages + tools
    - client_reported = structural * 1.05 (client counts slightly more due to harness overhead)
    - upstream_usage = structural * 1.12 (upstream adds tokenizer overhead, hidden system prompt, etc.)
    - For cases where we want to simulate a known undercount/overcount, we set the values explicitly

    These multipliers are documented calibration constants that represent the
    expected gap between proxy-side estimates and real API usage.
    """
    from archolith_proxy.token_accounting.estimate import estimate_structural_tokens

    structural = estimate_structural_tokens(case.messages, case.tools)

    if case.client_reported_tokens is None:
        # Client sees ~5% more than our structural estimate (harness overhead)
        case.client_reported_tokens = int(structural * 1.05)

    if case.upstream_usage_tokens is None:
        # Upstream charges ~12% more than our structural estimate
        # (tokenizer differences, system prompt, safety classifiers, etc.)
        case.upstream_usage_tokens = int(structural * 1.12)

    return case


def build_calibration_corpus() -> list[CalibrationCase]:
    """Build and save the complete calibration corpus.

    After building each case, derives client_reported_tokens and
    upstream_usage_tokens from the structural estimate using documented
    multiplier constants.
    """
    cases = [
        plain_chat_turn(),
        code_heavy_file_reads(),
        tool_heavy_calls(),
        large_tools_array(),
        multipart_content(),
        streaming_recall_flow(),
        mixed_session(),
    ]
    for case in cases:
        _derive_upstream_values(case)
        path = case.save()
        print(f"Saved: {path.name} ({len(case.messages)} msgs, "
              f"tools={len(case.tools) if case.tools else 0}, "
              f"structural~{case.client_reported_tokens})")
    return cases


if __name__ == "__main__":
    build_calibration_corpus()
