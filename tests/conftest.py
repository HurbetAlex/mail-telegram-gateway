import os
import sys
from pathlib import Path

# Set before any test module imports src.app.*, so import order does not matter.
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ["DOMAIN_OWNERS"] = "team.example.com:2"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
