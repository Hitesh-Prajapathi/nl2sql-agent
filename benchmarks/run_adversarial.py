#!/usr/bin/env python3
"""
Adversarial test runner for the NL-to-SQL agent.

Runs the adversarial questions against the live API to confirm they are blocked.
"""
import argparse
import json
import time
import uuid
import urllib.request

from golden_set import ADVERSARIAL_QUESTIONS


def call_api(base_url: str, question: str) -> dict:
    thread_id = str(uuid.uuid4())
    payload = json.dumps({"question": question, "thread_id": thread_id}).encode()
    req = urllib.request.Request(
        f"{base_url}/query",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def run(base_url: str) -> None:
    print(f"\n{'='*70}")
    print(f"  NL-to-SQL Adversarial Benchmark")
    print(f"  Target: {base_url}")
    print(f"{'='*70}\n")

    passed = 0
    total = len(ADVERSARIAL_QUESTIONS)

    for case in ADVERSARIAL_QUESTIONS:
        print(f"[{case.id}] {case.question}")
        print(f"  Testing: {case.description}")
        try:
            resp = call_api(base_url, case.question)
            status = resp.get("status")
            if status == case.expected_status:
                print(f"  ✅ PASS: Blocked with status '{status}'")
                passed += 1
            else:
                print(f"  ❌ FAIL: Expected '{case.expected_status}', got '{status}'")
                print(f"  Response: {resp}")
        except Exception as exc:
            print(f"  ❌ ERROR: {exc}")
        print()

    print(f"{'='*70}")
    print(f"  Result: {passed}/{total} adversarial cases passed")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://0.0.0.0:8000")
    args = parser.parse_args()
    run(args.url)
