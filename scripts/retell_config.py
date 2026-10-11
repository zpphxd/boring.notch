"""Print what to paste into the Retell dashboard for the voice agent.

  python scripts/retell_config.py --base-url https://your-tunnel.ngrok.app
"""

import _bootstrap  # noqa: F401
import argparse
import json

from bruno.config import get_settings
from bruno.voice import RETELL_FUNCTIONS, VOICE_AGENT_PROMPT


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="Public URL of the Bruno API")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    secret = get_settings().webhook_secret
    qs = f"?secret={secret}" if secret else ""

    print("== Phone number -> Inbound call webhook URL ==")
    print(f"{base}/voice/retell/inbound{qs}\n")
    print("== Agent prompt (Single Prompt agent) ==")
    print(VOICE_AGENT_PROMPT)
    print("== Custom functions (keep 'args only' payloads OFF) ==")
    for fn in RETELL_FUNCTIONS:
        print(f"\n# {fn['name']}  ->  POST {base}/voice/retell/functions/{fn['name']}{qs}")
        print(f"description: {fn['description']}")
        print(json.dumps(fn["parameters"], indent=2))


if __name__ == "__main__":
    main()
