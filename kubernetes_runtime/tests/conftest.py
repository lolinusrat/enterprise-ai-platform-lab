import os

# Settings are read at import time: keep warm-up and the CPU-bound tool fast in tests.
os.environ.setdefault("STARTUP_DELAY_S", "0.2")
os.environ.setdefault("RISK_ITERATIONS", "1000")
