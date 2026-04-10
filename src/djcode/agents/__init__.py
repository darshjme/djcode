"""DJcode agent system — specialized AI agents for different tasks.

Core modules:
  registry  — 18 PhD agent specs (AgentSpec, AgentRole, AGENT_SPECS)
  state     — Agent lifecycle state machine with event emission
  ra        — Research Assistant framework (pre-execution context gathering)
  executor  — Single-agent executor with full tool-calling loop
  parallel  — Multi-agent parallel/pipeline/wave coordinator
"""
