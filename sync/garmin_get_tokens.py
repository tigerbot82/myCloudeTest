#!/usr/bin/env python3
"""
Run this ONCE in Google Colab (colab.research.google.com) to generate
Garmin OAuth tokens. The output is a base64 string to store as the
GARMIN_TOKENS GitHub secret. The sync script uses it to authenticate
without triggering Garmin's cloud-IP rate limit.

Paste the entire contents of this file into a Colab cell and run it.
"""

import base64, getpass, json, os, tempfile

!pip install garminconnect -q
import garminconnect

email    = input("Garmin email: ").strip()
password = getpass.getpass("Garmin password: ")

print("\nLogging in to Garmin Connect...")

with tempfile.TemporaryDirectory() as tmpdir:
    api = garminconnect.Garmin(email, password)
    api.login()
    api.garth.dump(tmpdir)
    token_data = {}
    for fname in os.listdir(tmpdir):
        with open(os.path.join(tmpdir, fname)) as f:
            token_data[fname] = f.read()

blob = base64.b64encode(json.dumps(token_data).encode()).decode()

print("\n✓ Tokens generated.")
print("\n── Copy the line below as your GARMIN_TOKENS GitHub secret ──\n")
print(blob)
print("\n── end ──")
