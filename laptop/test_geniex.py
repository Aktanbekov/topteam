"""Check that GenieX is serving Qwen3-VL and will answer us.

This is the laptop-side half of the signal check - no UNO Q involved.

First, in a separate terminal, start the server:

    geniex serve

Then:

    python test_geniex.py
    python test_geniex.py --image frame.jpg    # also test the vision path
"""

import argparse
import base64
import json
import mimetypes
import sys
import time
from pathlib import Path

import requests

BASE_URL = "http://127.0.0.1:18181/v1"
MODEL = "qualcomm/Qwen3-VL-4B-Instruct:W4A16"
TIMEOUT_S = 180

# The scene question the real detector asks, imported rather than copied - a
# second copy drifts, and then the smoke test passes on a prompt we no longer
# ship. See tools/eval_scene_prompt.py for how this one was chosen.
from scene_vision import SCENE_PROMPT  # noqa: E402


def check_server():
    """Confirm the server is up and report which models it offers."""
    try:
        resp = requests.get(f"{BASE_URL}/models", timeout=10)
    except requests.RequestException as exc:
        sys.exit(
            f"GenieX is not answering at {BASE_URL}\n"
            f"  {exc}\n\n"
            "Start it in another terminal with:  geniex serve"
        )

    resp.raise_for_status()
    models = [m["id"] for m in resp.json().get("data", [])]
    print(f"server is up, {len(models)} model(s) available:")
    for name in models:
        print(f"  - {name}")
    return models


def as_data_url(path):
    """Encode an image file as a data URL for the OpenAI-style image content."""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


def ask(model, content, label):
    """Send one chat completion and print the reply with its latency."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 200,
        "temperature": 0,
    }

    print(f"\n{label}...")
    started = time.perf_counter()
    resp = requests.post(
        f"{BASE_URL}/chat/completions", json=payload, timeout=TIMEOUT_S
    )
    elapsed = time.perf_counter() - started

    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}: {resp.text[:400]}")
        return None

    reply = resp.json()["choices"][0]["message"]["content"]
    print(f"  {elapsed:.1f}s")
    print(f"  reply: {reply.strip()}")
    return reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        help="optional image to test the vision path with",
    )
    args = parser.parse_args()

    models = check_server()
    if args.model not in models:
        print(f"\nWARNING: {args.model} is not in the list above; trying it anyway.")

    ask(args.model, "Reply with exactly one word: ready", "text round trip")

    if args.image is None:
        print("\nText path works. Re-run with --image <file> to test vision.")
        return

    if not args.image.is_file():
        sys.exit(f"\nNo such image: {args.image}")

    reply = ask(
        args.model,
        [
            {"type": "text", "text": SCENE_PROMPT},
            {"type": "image_url", "image_url": {"url": as_data_url(args.image)}},
        ],
        f"vision round trip ({args.image.name})",
    )

    # The detector depends on parseable JSON, so check that here rather than
    # discovering it later inside the pipeline.
    if reply:
        try:
            print(f"  parsed JSON: {json.loads(reply.strip())}")
        except json.JSONDecodeError:
            print("  NOTE: reply was not clean JSON - the detector will need to")
            print("        strip markdown fences or extract the JSON substring.")


if __name__ == "__main__":
    main()
