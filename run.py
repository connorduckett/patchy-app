"""Run Patchy from source:  python run.py [--headless] [--data DIR] [sync|videohubs]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from patchy.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
