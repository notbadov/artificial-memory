# Codex thread modes

These mode profiles are the canonical, shareable way to switch between a high-quality Codex session and lower-burn variants.
They are defined semantically in `agent-platform/shared/modes` and rendered for Codex under `adapters/codex/profiles`. The rendered profiles are installed as separate files under `$CODEX_HOME` (`~/.codex` on macOS/Linux, `%USERPROFILE%\.codex` on Windows).

Use them with:

```bash
codex --profile optimal
codex --profile balanced-economy
codex --profile economy
```

## Mode contract

| Mode | Model | Reasoning effort | Verbosity | Intended behavior |
| --- | --- | --- | --- | --- |
| `optimal` | `gpt-5.6-terra` | `medium` | `medium` | Full-fidelity work. Use for production changes, escaped defects, and tasks that need deeper verification. |
| `balanced-economy` | `gpt-5.6-terra` | `low` | `low` | Default cost-saving mode when you still want strong code quality and a single verification pass. |
| `economy` | `gpt-5.6-luna` | `low` | `low` | Lowest-cost mode for narrow, repetitive, or exploratory tasks. Keep the task small and avoid repeated deep loops. |

The profile files only control model selection and output/effort posture. Approval, sandbox, hooks, and review discipline still come from the surrounding Codex configuration and task rules.

## Why these models

Official Codex/OpenAI guidance currently positions the `gpt-5.6` family like this:

- `gpt-5.6-sol` — flagship capability.
- `gpt-5.6-terra` — balance of intelligence and cost.
- `gpt-5.6-luna` — optimized for cost-sensitive workloads.

That makes `gpt-5.6-terra` the natural base for both `optimal` and `balanced-economy`, with `gpt-5.6-luna` reserved for the cheapest mode.

## Pricing reference

Official credit pricing per 1M tokens:

| Model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| `gpt-5.6-sol` | 125 | 12.5 | 750 |
| `gpt-5.6-terra` | 50 | 5 | 300 |
| `gpt-5.6-luna` | 5 | 0.5 | 30 |
| `gpt-5.5` | 125 | 12.5 | 750 |
| `gpt-5.4` | 62.5 | 6.25 | 375 |
| `gpt-5.4 mini` | 18.75 | 1.875 | 113 |

There is no official fixed multiplier per reasoning-effort level. Lower effort generally reduces reasoning-token burn and often shortens output, but the exact cost depends on the task.

## Why `gpt-5.5` and `gpt-5.4 mini` are not canonical modes

They are still usable models, but they are not the cleanest default mode split for this setup:

- `gpt-5.5` sits in the same official pricing band as `gpt-5.6-sol`, so it does not give a cost-saving tier by itself.
- `gpt-5.4 mini` is cheaper, but the `gpt-5.6` family gives a clearer and more current split between balanced quality and low-cost operation.

If we later want extra compatibility profiles, they can be added as separate profiles without changing the canonical three-mode contract.

## Installation

Use the Codex adapter installer from `agent-platform` to copy the rendered profile files into your local Codex home:

```bash
python3 adapters/codex/scripts/install_codex_thread_modes.py --codex-root "$HOME/.codex"
```

On Windows, the same script can be run with the Windows Codex home path:

```powershell
python .\adapters\codex\scripts\install_codex_thread_modes.py --codex-root $env:USERPROFILE\.codex
```
