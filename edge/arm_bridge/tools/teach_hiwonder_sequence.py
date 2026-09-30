#!/usr/bin/env python3
"""Record a Hiwonder bus-servo arm pick sequence from physical poses.

Run this on the Raspberry Pi with the vendor ``ros_robot_controller_sdk.py``
next to it. Move the arm to each pose, press Enter to record that pose, then
press ``q`` to save. The output JSON is accepted by
``HiwonderBusServoArmDriver`` through ``pick_sequence_file``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/ttyAMA0")
    parser.add_argument("--baudrate", type=int, default=1000000)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--servo-ids", type=int, nargs="+", required=True)
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--output", default="pick_sequence.json")
    return parser.parse_args(argv)


def read_positions(board: Any, servo_ids: list[int]) -> list[list[int]]:
    """Read current 0..1000 positions for every servo id."""
    positions: list[list[int]] = []
    for servo_id in servo_ids:
        value = board.bus_servo_read_position(servo_id)
        if not value:
            raise RuntimeError(
                f"servo {servo_id} returned no position; check wiring and "
                "enable_reception"
            )
        position = int(value[0])
        if not 0 <= position <= 1000:
            raise RuntimeError(
                f"servo {servo_id} reported position {position}, expected 0..1000"
            )
        positions.append([servo_id, position])
    return positions


def build_output(
    servo_ids: list[int],
    steps: list[dict[str, Any]],
    duration: float,
) -> dict[str, Any]:
    """Build the JSON document consumed by the driver's sequence loader."""
    return {
        "format": "seasight_hiwonder_pick_sequence",
        "version": 1,
        "servo_ids": list(servo_ids),
        "default_duration": float(duration),
        "steps": steps,
    }


def record_loop(
    board: Any,
    servo_ids: list[int],
    duration: float,
    output: str,
) -> int:
    steps: list[dict[str, Any]] = []
    print("Move the arm to each pose, then press Enter to record it.")
    print("Press q to save and exit.")
    while True:
        try:
            user_input = input(
                "Enter to record current pose, q to save and exit: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if user_input == "q":
            break
        positions = read_positions(board, servo_ids)
        steps.append({"duration": float(duration), "positions": positions})
        print(f"recorded step {len(steps)}: {positions}")
    if not steps:
        print("no steps recorded; nothing written", file=sys.stderr)
        return 1
    payload = build_output(servo_ids, steps, duration)
    Path(output).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"saved {len(steps)} steps to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration <= 0:
        print("--duration must be > 0", file=sys.stderr)
        return 2
    try:
        import ros_robot_controller_sdk as rrc  # noqa: PLC0415
    except ImportError as exc:
        print(
            "vendor SDK not found; copy ros_robot_controller_sdk.py next to "
            "this script and install pyserial",
            file=sys.stderr,
        )
        return 2
    board = rrc.Board(
        device=args.device,
        baudrate=args.baudrate,
        timeout=args.timeout,
    )
    board.enable_reception(True)
    try:
        return record_loop(board, args.servo_ids, args.duration, args.output)
    except Exception as exc:  # noqa: BLE001
        print(f"recording failed: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            board.bus_servo_stop(args.servo_ids)
            for servo_id in args.servo_ids:
                board.bus_servo_enable_torque(servo_id, False)
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
