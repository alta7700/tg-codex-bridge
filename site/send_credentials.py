#!/usr/bin/env python3
"""Send the newly authorized site login via the installed tg-send implementation."""
import json
import sys
import io
import argparse
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    args = parser.parse_args()
    result = json.loads((ROOT / "site/build/deployment-validation.json").read_text())
    local = json.loads((ROOT / "site/dist/release.json").read_text())
    assert result["state"] == "PASS" and result["release_id"] == local["release_id"]
    p = ROOT / ".runtime/sud-home-private"
    credentials = json.loads((p / "credentials.json").read_text())
    ui = json.loads((ROOT / "site/content/ui.json").read_text())
    assert result["url"] == credentials["url"]
    text = ("Сайт готов: " + ui["hearing_title"] + "\n" + result["url"] + "\n\n"
            "Логин: " + credentials["username"] + "\nПароль: " + credentials["password"] + "\n\n"
            "Вход общий для участников. Начните с «" + ui["hearing_title"] + "»; цитата открывает исходную страницу. "
            "Далее — все вопросы, финансовая сверка, поправки к прежним выводам и PDF.")
    # Arguments exist only inside this Python process, never in the shell command.
    sys.argv = ["tg-send", "--socket", args.socket,
                "--chat-id", str(args.chat_id), "--request-id", "sud-home-initial-credentials-v1", "--text", text]
    from tg_bridge.cli import send, ipc_request
    capture = io.StringIO()
    with redirect_stdout(capture):
        send()
    receipt = json.loads(capture.getvalue())
    assert receipt["ok"] and receipt["message_ids"]
    # Do not store the login message in an ordinary report or duplicate it on retries.
    (p / "credential-send-completed.json").write_text(json.dumps({
        "state":"CLI_confirmed_pending_history_check","request_id":"sud-home-initial-credentials-v1",
        "release_id":local["release_id"],"message_ids":receipt["message_ids"]}) + "\n")
    capture = io.StringIO()
    with redirect_stdout(capture):
        ipc_request(SimpleNamespace(socket=args.socket,
                                    request_id="sud-home-credentials-history-check-v1"),
                    {"op":"history","chat_id":args.chat_id,"message_id":receipt["message_ids"][0],"limit":5})
    history = json.loads(capture.getvalue())
    assert history["ok"]
    sent = next(m for m in history["messages"] if m["message_id"] == receipt["message_ids"][0])
    assert sent["direction"] == "out" and sent["chat_id"] == args.chat_id and sent["text"] == text
    safe = {"state":"PASS_delivery_confirmed_in_history","release_id":local["release_id"],
            "message_ids":receipt["message_ids"],"request_id":"sud-home-initial-credentials-v1",
            "url":result["url"],"credential_output":False}
    (ROOT / "site/build/credential-delivery.json").write_text(json.dumps(safe,indent=2) + "\n")
    print(json.dumps(safe))


if __name__ == "__main__":
    main()
