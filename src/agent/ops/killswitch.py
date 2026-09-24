"""Kill switch (architecture.md §4.7, §4.11): blocks new entries; exits stay allowed.

Two ways to engage it:
  manual  the flag file data/KILL exists (create it by hand, later via Telegram /kill). It stays engaged, across
          days and restarts, until the file is deleted.
  auto    engage(reason) on a daily-loss or consecutive-loss breach. Recorded in data/killswitch.json with the
          day, so a restart later that day stays killed; the next trading day starts clear.

With directory=None nothing touches disk (backtests): only the auto part exists, in memory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent.broker.fyers_auth import PROJECT_ROOT

DEFAULT_DIR = PROJECT_ROOT / "data"
MANUAL_FLAG = "KILL"
AUTO_FILE = "killswitch.json"


@dataclass(frozen=True)
class KillStatus:
    engaged: bool
    reason: str | None = None
    source: str | None = None  # 'manual' | 'auto'


class KillSwitch:
    def __init__(self, directory: Path | None = DEFAULT_DIR, journal=None):
        self.directory = Path(directory) if directory is not None else None
        self.journal = journal
        self._auto: dict | None = None  # {"day", "reason", "engaged_at"}
        if self.directory is not None:
            path = self.directory / AUTO_FILE
            if path.exists():
                self._auto = json.loads(path.read_text(encoding="utf-8"))

    def status(self, now: datetime) -> KillStatus:
        if self.directory is not None and (self.directory / MANUAL_FLAG).exists():
            text = (self.directory / MANUAL_FLAG).read_text(encoding="utf-8").strip()
            return KillStatus(True, text or "manual kill flag present", "manual")
        if self._auto and self._auto["day"] == now.date().isoformat():
            return KillStatus(True, self._auto["reason"], "auto")
        return KillStatus(False)

    def engage(self, reason: str, now: datetime) -> None:
        """Engage for the rest of `now`'s day. Already engaged today → the first reason is kept."""
        if self._auto and self._auto["day"] == now.date().isoformat():
            return
        self._auto = {"day": now.date().isoformat(), "reason": reason, "engaged_at": now.isoformat()}
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self.directory / (AUTO_FILE + ".tmp")
            tmp.write_text(json.dumps(self._auto), encoding="utf-8")
            os.replace(tmp, self.directory / AUTO_FILE)
        if self.journal is not None:
            self.journal.log_event("KILL_SWITCH", "CRITICAL", reason, ts=now)
