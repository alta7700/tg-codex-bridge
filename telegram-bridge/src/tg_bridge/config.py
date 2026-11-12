from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def ipc_directory(root):
    import hashlib
    tag = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return Path(f"/tmp/tg-bridge-{os.getuid()}-{tag}")


@dataclass(frozen=True)
class Config:
    root: Path
    workspace: Path
    chat_id: int
    user_ids: frozenset[int]
    token_file: Path
    codex: str
    codex_socket: Path
    thread_id: str

    @property
    def state(self):
        return self.root / ".state"

    @property
    def socket(self):
        # macOS Unix sockets have a 104-byte limit; keep IPC in a private short path.
        return ipc_directory(self.root) / "bridge.sock"

    @property
    def output(self):
        return self.workspace / "output" / "telegram"

    @property
    def attachments(self):
        return self.workspace / "tg_input"

    @classmethod
    def load(cls, root=ROOT):
        root = Path(root).resolve()
        # Desktop sets CODEX_THREAD_ID to the caller's chat. The bridge's explicit
        # binding must win over that inherited environment value.
        values = {**os.environ, **{k: v for k, v in dotenv_values(root / ".env").items() if v is not None}}
        def path(key, default):
            p = Path(values.get(key) or default).expanduser()
            return (root / p).resolve() if not p.is_absolute() else p.resolve()
        cfg = cls(root, path("WORKSPACE", root.parent), int(values.get("TG_CHAT_ID") or "0"),
                  frozenset(int(x.strip()) for x in (values.get("TG_USER_IDS") or "").split(",") if x.strip()),
                  path("TG_TOKEN_FILE", "private/bot-token"), values.get("CODEX_BIN") or "codex",
                  path("CODEX_SOCKET", ipc_directory(root) / "desktop-app-server.sock"),
                  values.get("CODEX_THREAD_ID") or "")
        if not cfg.chat_id or not cfg.thread_id:
            raise ValueError("Set TG_CHAT_ID and CODEX_THREAD_ID in .env")
        if not cfg.workspace.is_dir():
            raise ValueError("WORKSPACE must exist")
        return cfg

    def prepare(self):
        for p in [self.state, self.attachments, self.socket.parent, self.token_file.parent]:
            if p.is_symlink():
                raise ValueError("Private directory cannot be a symlink")
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
            if p.stat().st_uid != os.getuid():
                raise ValueError("Private directory has a different owner")
            p.chmod(0o700)
        self.output.mkdir(parents=True, exist_ok=True)

    def authorized(self, chat, user):
        return chat == self.chat_id and user is not None and (not self.user_ids or user in self.user_ids)

    def thread_params(self):
        """Add transport instructions while preserving Desktop's runtime settings."""
        instructions = (self.root / "TELEGRAM.md").read_text()
        instructions += (f"\nWorkspace: {self.workspace}\nSuggested report directory: {self.output}\n"
                         f"Incoming attachments: {self.attachments}\nAllowed chat_id: {self.chat_id}\n"
                         f"CLI: {self.root / '.venv/bin/tg-send'}, {self.root / '.venv/bin/tg-send-file'}, {self.root / '.venv/bin/tg-history'}, {self.root / '.venv/bin/tg-ignore'}\n"
                         f"Pass --socket {self.socket} to each CLI invocation.\n")
        return {"developerInstructions": instructions}
