import json
from pathlib import Path
from typing import Any


class SessionManager:
    def __init__(self):
        # Store config in ~/.config/surgical_context_engine/session.json
        self.config_dir = Path.home() / ".config" / "surgical_context_engine"
        self.config_file = self.config_dir / "session.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def save_token(self, token: str):
        with open(self.config_file, "w") as f:
            json.dump({"github_token": token}, f)

    def get_token(self) -> str | Any:
        if not self.config_file.exists():
            return None
        with open(self.config_file) as f:
            data = json.load(f)
            return data.get("github_token")

    def logout(self):
        if self.config_file.exists():
            self.config_file.unlink()
