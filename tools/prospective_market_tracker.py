"""Offline closing-quote and contract calibration commands for the prospective registry."""

import argparse
import json
from pathlib import Path

from bet_engine.modeling.prospective_market_tracking import attach_closing_odds, evaluate_market_tracking


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    closing = commands.add_parser("attach-closing")
    closing.add_argument("--record", type=Path, required=True)
    closing.add_argument("--bwin-txt", type=Path, required=True)
    closing.add_argument("--snapshot-sha256", required=True)
    closing.add_argument("--max-lead-minutes", type=int, default=20)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--records-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "attach-closing":
            result = attach_closing_odds(args.record, args.bwin_txt, expected_sha256=args.snapshot_sha256,
                                         max_lead_minutes=args.max_lead_minutes)
        else:
            result = evaluate_market_tracking(args.records_dir)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
