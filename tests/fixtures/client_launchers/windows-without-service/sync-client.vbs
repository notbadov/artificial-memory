Option Explicit
Dim shell, result
Set shell = CreateObject("WScript.Shell")
result = shell.Run("""{ROOT}/system python/python/bin/python3"" ""{ROOT}/home of O'Neil & Сын/.agent-platform/client/sync_client.py"" --client-id golden-computer --display-name ""Мария's \""Mac\"" & PC"" --update-policy automatic --control-plane-url """" --release-trust-file ""{ROOT}/home of O'Neil & Сын/trust.json"" --release-cache ""{ROOT}/home of O'Neil & Сын/cache"" --codex-root ""{ROOT}/home of O'Neil & Сын/.codex"" --claude-root ""{ROOT}/home of O'Neil & Сын/.claude"" --claude-config ""{ROOT}/home of O'Neil & Сын/.claude.json"" --bin-dir ""{ROOT}/home of O'Neil & Сын/.local/bin"" --project-mappings ""{ROOT}/home of O'Neil & Сын/.agent-platform/project-mappings.json"" --provider codex", 0, True)
WScript.Quit result
