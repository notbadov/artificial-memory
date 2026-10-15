# OpenCode adapter

The OpenCode adapter installs the provider-neutral Agent Platform into OpenCode without making OpenCode or a model runtime the source of truth.

It installs:

- shared policies through OpenCode's global `instructions` configuration;
- portable skills into the global OpenCode skills directory;
- Agent Memory as a remote MCP server;
- an Agent Platform plugin for lifecycle environment, compaction continuity, and the escaped-defect completion gate;
- semantic modes and the `opencode-mode` launcher;
- locally discovered Ollama models as an optional provider.

The installer preserves unrelated OpenCode configuration. It never writes credentials to the repository or release bundle, and it never selects a default model. Mode-to-model mappings are machine-local and must be set explicitly with `--mode-model MODE=PROVIDER/MODEL`.

The synchronization client runs `install.py` from the release it has verified. The skills, policies and modes themselves are not in this repository: they arrive in the signed release.

After installation restart OpenCode. To launch a semantic mode:

```sh
opencode-mode balanced-economy
```

For Ollama tool use, configure the selected model with a sufficiently large context window (normally 16K–32K or more).
