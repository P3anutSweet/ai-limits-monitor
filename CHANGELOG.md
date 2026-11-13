# Changelog

## 2026-11-13: first public release

- Glass desktop panel (per-pixel alpha), compact and full views, animated meters with an even-pace tick, in-use halos, Hide with Ctrl+Alt+H.
- Claude and Codex usage limits from the apps' own (unofficial) usage endpoints. Gemini windows via T3 Code's Antigravity cache, DeepSeek balance, and sign-in status cards for Grok, Kimi, Qwen, Doubao, GLM and MiniMax.
- Settings file `ai_limits_settings.json` plus `AI_LIMITS_<KEY>` environment overrides. Defaults: local time zone, data in `%LOCALAPPDATA%\ai-limits-monitor`, five services.
- `--demo` (sample data, plain background, nothing saved). `--snapshot` runs beside a running panel without updating state, caches or `latest.json` and without sending alerts; it writes only the requested PNG. `--backdrop` flattens the PNG onto a plain card.
- Unofficial AI-made mascot illustrations for each service (CC BY 4.0, with prompts and provenance in `avatars/`). Claude, GPT, Grok and DeepSeek were redrawn on 2026-11-13 from text-only prompts.
