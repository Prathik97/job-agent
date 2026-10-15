"""
smoke_test.py
Run after setup:  python smoke_test.py
Checks: (1) your two config files load, (2) your API key works, (3) both models respond.
"""
import json
import os
import sys

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5-5"


def check_files():
    with open("profile.yaml") as f:
        profile = yaml.safe_load(f)
    with open("master_profile.json") as f:
        master = json.load(f)

    n_ach = sum(len(role["achievements"]) for role in master["experience"])
    print(f"[ok] profile.yaml loaded. Tier A titles: {len(profile['titles']['tier_a'])}")
    print(f"[ok] master_profile.json loaded. Roles: {len(master['experience'])}, achievements: {n_ach}")

    if profile["compensation"]["floor_lpa_inr"] is None:
        print("[todo] compensation.floor_lpa_inr is empty in profile.yaml")


def check_models():
    load_dotenv()
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        print("[fail] ANTHROPIC_API_KEY missing. Copy .env.example to .env and add your key.")
        sys.exit(1)

    client = Anthropic(api_key=key)
    for label, model in [("Haiku", HAIKU), ("Sonnet", SONNET)]:
        resp = client.messages.create(
            model=model,
            max_tokens=30,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        print(f"[ok] {label} ({model}) says: {resp.content[0].text.strip()}")


if __name__ == "__main__":
    check_files()
    check_models()
    print("\nAll checks passed. Step 3 is done.")
