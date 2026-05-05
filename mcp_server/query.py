"""
CLI query tool for health + gym Firestore data.
Usage:
  python query.py summary [days]
  python query.py trend <metric> [days]
  python query.py raw [days]
  python query.py gym [n]
"""

import sys
import os

os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "/home/user/.firebase-key.json")
sys.path.insert(0, os.path.dirname(__file__))
import health_server as s

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    if cmd == "summary":
        days = int(args[1]) if len(args) > 1 else 30
        print(s.get_health_summary(days))
    elif cmd == "trend":
        if len(args) < 2:
            print("Usage: trend <metric> [days]")
            return
        metric = args[1]
        days = int(args[2]) if len(args) > 2 else 60
        print(s.get_metric_trend(metric, days))
    elif cmd == "raw":
        days = int(args[1]) if len(args) > 1 else 14
        print(s.get_health_days(days))
    elif cmd == "gym":
        n = int(args[1]) if len(args) > 1 else 20
        print(s.get_gym_sessions(n))
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

if __name__ == "__main__":
    main()
