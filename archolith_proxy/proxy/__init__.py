"""Proxy core — session management, message rewriting, tool interception, circuit breakers.

Main submodules:
- session: Session fingerprinting and resolution
- locks: Per-session asyncio.Lock management for extraction ordering
- recall: __archolith_recall tool call interception and context recall
- rewrite: Message array rewriting and token estimation
- streaming: Streaming response handling with output filtering
- synthetic_tools: Agent-initiated tool injection and circuit breaking
- tool_intercept: Native read and tool interception for file/tool caching
- tool_injection: Tool definition injection and argument mapping
- agent_solo: Agent-solo turn compression via archolith_filter
- upstream: Retry logic and upstream request handling
- live: Event broadcasting for live session inspection
- circuit_breaker: Circuit breaker state management
"""
