# Global agent bootstrap rules

This file is generated from `agent-platform`. Reusable skills and shared policy
come from `agent-platform/shared`; do not edit installed `~/.claude` copies as
their source of truth.

Use the `agentMemory` MCP server whenever global preferences, project-specific
architecture, previous decisions, recurring mistakes, or preferred prompts may
exist outside the repository. Search first, then fetch the exact note before
using it as evidence. Treat search results as an index, not as note contents.

When a relevant Agent Memory capability is not visible, inspect the configured
MCP tools before concluding it is unavailable. Durable semantic learning must
go through a reviewable proposal rather than a direct unreviewed write.

The current semantic work mode is supplied by the `claude-mode` launcher through
`AGENT_PLATFORM_MODE`. Its model and effort mapping is provider-specific, while
the quality gates in `shared/modes` remain common across agents.
