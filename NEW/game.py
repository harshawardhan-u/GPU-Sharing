"""
The demo game. This is a thin entry point around remote_play/app.py so the
demo narration is literally:

    python game.py            <- fails on a non-GPU laptop (no NVIDIA GPU)
    cloud-run game.py         <- works: connects to a GPU host over the LAN
                                  and streams it live, controllable here

To actually HOST it (run on the GPU laptop so others can connect):
    python game.py --serve
"""

import os
import runpy
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_remote_play_dir = os.path.join(_here, "remote_play")
_app_script = os.path.join(_remote_play_dir, "app.py")

sys.path.insert(0, _remote_play_dir)
os.chdir(_remote_play_dir)

runpy.run_path(_app_script, run_name="__main__")
