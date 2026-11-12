# Telegram bridge for Codex Desktop

A local bridge between one Telegram chat and one existing Codex Desktop chat.
Messages, attachments, history and replies share the same workspace. There is no
cloud deployment. Requires macOS, Codex Desktop, Python 3.11+ and a Telegram bot.

The implementation and setup guide are in [telegram-bridge](telegram-bridge/README.md).
Persistent operator instructions are in [AGENTS.md](AGENTS.md); operational context
for a replacement agent is in [HANDOFF.md](telegram-bridge/HANDOFF.md).
Bridge fixes and new capabilities use the repository skill
[improve-telegram-bridge](.agents/skills/improve-telegram-bridge/SKILL.md).

This checkout can live in a larger data workspace. Git tracks only the source
allowlist: documents, media, runtime environments, model weights, chat history,
local configuration and secrets remain outside version control.
