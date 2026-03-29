# Benchmarks

Adapters for running the harness agent on standard evaluation benchmarks.

## Terminal-Bench 2.0

```bash
# Install
uv tool install terminal-bench

# Single task test
tb run --agent-import-path benchmarks.harbor_agent:HarnessBaseAgent --task-id hello-world

# Full benchmark (5 trials per task)
tb run --agent-import-path benchmarks.harbor_agent:HarnessBaseAgent -k 5
```

See `harbor_agent.py` for details on the two integration options (BaseAgent vs InstalledAgent).
