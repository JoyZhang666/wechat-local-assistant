"""Stable command entry, independent of the developer's machine directories."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.worker import main

if __name__=="__main__":
    raise SystemExit(main())
