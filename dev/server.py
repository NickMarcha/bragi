"""Bragi on a fake PipeWire, for driving the Android app from an emulator.

Nothing here talks to a real audio graph: `pipewire._run` is the fake sagepi
host from the tests, and the background tasks that shell out to pw-mon,
pw-cat, or /dev/input sit idle. Registration, the dashboard, and the peer
control socket are the real code. Audio is checked separately with the Roc
CLI tools (dev/android-audio.sh), standing in for sagepi's Roc modules.

Run through dev/compose.yml, which sets BRAGI_LOCAL_DEV, BRAGI_DATA_DIR,
and BRAGI_ROC_PORT_BASE before this imports the app.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import knob_watcher, level_meter, pipewire, watcher  # noqa: E402
from tests import fake_pipewire  # noqa: E402


async def _idle(*_args) -> None:
    await asyncio.Event().wait()


pipewire._run = fake_pipewire.sagepi_session().run
watcher.watch = knob_watcher.watch = level_meter.supervise = _idle

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
