#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont


CANVAS_WIDTH = 1200
CANVAS_HEIGHT = 720
MARGIN_LEFT = 90
MARGIN_RIGHT = 40
MARGIN_TOP = 70
MARGIN_BOTTOM = 90
BACKGROUND = "#f7f8fb"
TEXT = "#1f2937"
MUTED = "#6b7280"
GRID = "#d8dce5"
PRIMARY = "#2563eb"
SECONDARY = "#dc2626"
ACCENT = "#0f766e"
BAR = "#4f46e5"
BAR_ALT = "#0891b2"
HIST = "#7c3aed"
HIST_ALT = "#d97706"


@dataclass(frozen=True)
class Record:
    raw: dict[str, Any]

    @property
    def task(self) -> str:
        return str(self.raw.get("task", "")).strip() or "unknown"

    @property
    def status(self) -> str:
        return str(self.raw.get("status", "")).strip() or "unknown"

    @property
    def model(self) -> str:
        return str(self.raw.get("model", "")).strip() or "unknown"

    @property
    def metadata(self) -> dict[str, Any]:
        value = self.raw.get("metadata")
        return value if isinstance(value, dict) else {}

    @property
    def recorded_at(self) -> datetime | None:
        raw_value = str(self.raw.get("recorded_at", "")).strip()
        if not raw_value:
            return None
        normalized = raw_value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def int(self, key: str) -> int:
        value = self.raw.get(key, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def float(self, key: str) -> float:
        value = self.raw.get(key, 0.0)
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0


def load_records(path: Path) -> list[Record]:
    records: list[Record] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(Record(payload))
    return records


def font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", size)
    except OSError:
        return ImageFont.load_default()


def new_canvas(title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((MARGIN_LEFT, 24), title, fill=TEXT, font=font(28))
    draw.text((MARGIN_LEFT, 52), subtitle, fill=MUTED, font=font(16))
    return image, draw


def chart_bounds() -> tuple[int, int, int, int]:
    left = MARGIN_LEFT
    top = MARGIN_TOP + 30
    right = CANVAS_WIDTH - MARGIN_RIGHT
    bottom = CANVAS_HEIGHT - MARGIN_BOTTOM
    return left, top, right, bottom


def draw_axes(draw: ImageDraw.ImageDraw, y_label: str = "", x_label: str = "") -> tuple[int, int, int, int]:
    left, top, right, bottom = chart_bounds()
    draw.line((left, top, left, bottom), fill=TEXT, width=2)
    draw.line((left, bottom, right, bottom), fill=TEXT, width=2)
    if y_label:
        draw.text((left, top - 26), y_label, fill=MUTED, font=font(14))
    if x_label:
        draw.text((right - 160, bottom + 20), x_label, fill=MUTED, font=font(14))
    return left, top, right, bottom


def draw_grid(draw: ImageDraw.ImageDraw, max_value: float, steps: int = 5) -> list[float]:
    left, top, right, bottom = chart_bounds()
    values: list[float] = []
    for idx in range(steps + 1):
        ratio = idx / steps
        y = bottom - ((bottom - top) * ratio)
        value = max_value * ratio
        draw.line((left, y, right, y), fill=GRID, width=1)
        label = f"{value:,.0f}"
        bbox = draw.textbbox((0, 0), label, font=font(13))
        draw.text((left - (bbox[2] - bbox[0]) - 10, y - 8), label, fill=MUTED, font=font(13))
        values.append(value)
    return values


def save_chart(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def write_csv(path: Path, headers: list[str], rows: Iterable[Iterable[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def bar_chart(path: Path, title: str, subtitle: str, labels: list[str], values: list[float], color: str = BAR) -> None:
    image, draw = new_canvas(title, subtitle)
    left, top, right, bottom = draw_axes(draw, y_label="Value")
    max_value = max(values) if values else 1.0
    draw_grid(draw, max_value)
    bar_area_width = right - left
    count = max(len(values), 1)
    slot_width = bar_area_width / count
    bar_width = max(18, int(slot_width * 0.6))

    for index, (label, value) in enumerate(zip(labels, values)):
        x_center = left + slot_width * index + slot_width / 2
        x0 = int(x_center - bar_width / 2)
        x1 = int(x_center + bar_width / 2)
        height = 0 if max_value <= 0 else (value / max_value) * (bottom - top)
        y0 = int(bottom - height)
        draw.rounded_rectangle((x0, y0, x1, bottom), radius=6, fill=color)
        value_text = f"{value:,.0f}"
        bbox = draw.textbbox((0, 0), value_text, font=font(13))
        draw.text((x_center - (bbox[2] - bbox[0]) / 2, y0 - 22), value_text, fill=TEXT, font=font(13))
        label_bbox = draw.textbbox((0, 0), label, font=font(13))
        label_x = x_center - (label_bbox[2] - label_bbox[0]) / 2
        draw.text((label_x, bottom + 10), label, fill=TEXT, font=font(13))

    save_chart(image, path)


def histogram(path: Path, title: str, subtitle: str, values: list[int], bins: int = 10) -> None:
    image, draw = new_canvas(title, subtitle)
    left, top, right, bottom = draw_axes(draw, y_label="Calls", x_label="Input tokens")
    if not values:
        draw.text((left, top + 40), "No data.", fill=MUTED, font=font(18))
        save_chart(image, path)
        return

    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        maximum += 1
    bucket_size = max(1, math.ceil((maximum - minimum) / bins))
    bucket_counts = [0 for _ in range(bins)]
    bucket_labels: list[str] = []
    for bucket in range(bins):
        bucket_start = minimum + bucket * bucket_size
        bucket_end = bucket_start + bucket_size - 1
        bucket_labels.append(f"{bucket_start}-{bucket_end}")

    for value in values:
        bucket_index = min((value - minimum) // bucket_size, bins - 1)
        bucket_counts[int(bucket_index)] += 1

    max_count = max(bucket_counts) if bucket_counts else 1
    draw_grid(draw, max_count)
    slot_width = (right - left) / bins
    bar_width = max(10, int(slot_width * 0.7))

    for index, count in enumerate(bucket_counts):
        x_center = left + slot_width * index + slot_width / 2
        x0 = int(x_center - bar_width / 2)
        x1 = int(x_center + bar_width / 2)
        height = 0 if max_count <= 0 else (count / max_count) * (bottom - top)
        y0 = int(bottom - height)
        draw.rounded_rectangle((x0, y0, x1, bottom), radius=4, fill=HIST)
        label = bucket_labels[index]
        if index % 2 == 0:
            draw.text((x0, bottom + 10), label, fill=MUTED, font=font(11))

    save_chart(image, path)


def line_chart(path: Path, title: str, subtitle: str, labels: list[str], values: list[float], color: str = PRIMARY) -> None:
    image, draw = new_canvas(title, subtitle)
    left, top, right, bottom = draw_axes(draw, y_label="Value")
    if not values:
        draw.text((left, top + 40), "No data.", fill=MUTED, font=font(18))
        save_chart(image, path)
        return

    max_value = max(values) if values else 1.0
    draw_grid(draw, max_value)
    count = len(values)
    x_step = (right - left) / max(count - 1, 1)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(values):
        x = left + index * x_step
        y = bottom if max_value <= 0 else bottom - ((value / max_value) * (bottom - top))
        points.append((x, y))

    for index in range(1, len(points)):
        draw.line((*points[index - 1], *points[index]), fill=color, width=4)
    for index, (x, y) in enumerate(points):
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
        if count <= 12 or index % max(1, count // 8) == 0:
            draw.text((x - 20, bottom + 10), labels[index], fill=MUTED, font=font(11))

    save_chart(image, path)


def scatter_with_average_line(
    path: Path,
    title: str,
    subtitle: str,
    points: list[tuple[int, int]],
    averages: list[tuple[int, float]],
) -> None:
    image, draw = new_canvas(title, subtitle)
    left, top, right, bottom = draw_axes(draw, y_label="Input tokens", x_label="History messages")
    if not points:
        draw.text((left, top + 40), "No chat records with history metadata.", fill=MUTED, font=font(18))
        save_chart(image, path)
        return

    max_x = max(point[0] for point in points)
    max_y = max(point[1] for point in points)
    draw_grid(draw, max_y)
    x_scale = (right - left) / max(max_x, 1)

    for x_value, y_value in points:
        x = left + x_value * x_scale
        y = bottom - ((y_value / max_y) * (bottom - top))
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=BAR_ALT)

    if averages:
        average_points: list[tuple[float, float]] = []
        for x_value, y_value in averages:
            x = left + x_value * x_scale
            y = bottom - ((y_value / max_y) * (bottom - top))
            average_points.append((x, y))
            draw.text((x - 10, bottom + 10), str(x_value), fill=MUTED, font=font(11))
        for index in range(1, len(average_points)):
            draw.line((*average_points[index - 1], *average_points[index]), fill=SECONDARY, width=4)
        for x, y in average_points:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=SECONDARY)

    save_chart(image, path)


def comparison_bars(
    path: Path,
    title: str,
    subtitle: str,
    labels: list[str],
    before_values: list[float],
    after_values: list[float],
) -> None:
    image, draw = new_canvas(title, subtitle)
    left, top, right, bottom = draw_axes(draw, y_label="Value")
    max_value = max(before_values + after_values) if (before_values or after_values) else 1.0
    draw_grid(draw, max_value)
    slot_width = (right - left) / max(len(labels), 1)
    bar_width = max(16, int(slot_width * 0.24))

    for index, label in enumerate(labels):
        x_center = left + slot_width * index + slot_width / 2
        before_value = before_values[index]
        after_value = after_values[index]

        for offset, value, color in [(-bar_width, before_value, HIST_ALT), (bar_width, after_value, PRIMARY)]:
            x0 = int(x_center + offset - bar_width / 2)
            x1 = int(x_center + offset + bar_width / 2)
            height = 0 if max_value <= 0 else (value / max_value) * (bottom - top)
            y0 = int(bottom - height)
            draw.rounded_rectangle((x0, y0, x1, bottom), radius=5, fill=color)

        draw.text((x_center - 24, bottom + 10), label, fill=TEXT, font=font(12))

    draw.text((left, top - 28), "Before", fill=HIST_ALT, font=font(14))
    draw.text((left + 70, top - 28), "After", fill=PRIMARY, font=font(14))
    save_chart(image, path)


def mean_or_zero(values: list[float | int]) -> float:
    return float(statistics.mean(values)) if values else 0.0


def p90(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return int(ordered[index])


def aggregate_chat(records: list[Record]) -> dict[str, Any]:
    chat_records = [record for record in records if record.task == "chat" and record.status == "ok"]
    inputs = [record.int("input_tokens") for record in chat_records]
    outputs = [record.int("output_tokens") for record in chat_records]
    totals = [record.int("total_tokens") for record in chat_records]

    by_history: dict[int, list[Record]] = defaultdict(list)
    for record in chat_records:
        history_messages = record.metadata.get("history_messages")
        try:
            history_int = int(history_messages)
        except (TypeError, ValueError):
            continue
        by_history[history_int].append(record)

    return {
        "records": chat_records,
        "inputs": inputs,
        "outputs": outputs,
        "totals": totals,
        "avg_input": mean_or_zero(inputs),
        "avg_output": mean_or_zero(outputs),
        "avg_total": mean_or_zero(totals),
        "median_input": statistics.median(inputs) if inputs else 0,
        "p90_input": p90(inputs),
        "by_history": by_history,
    }


def total_tokens_by_task(records: list[Record]) -> list[tuple[str, int]]:
    totals: dict[str, int] = defaultdict(int)
    for record in records:
        if record.status != "ok":
            continue
        totals[record.task] += record.int("total_tokens")
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def cost_by_hour(records: list[Record]) -> list[tuple[str, float]]:
    buckets: dict[str, float] = defaultdict(float)
    for record in records:
        if record.status != "ok":
            continue
        recorded_at = record.recorded_at
        if recorded_at is None:
            continue
        buckets[recorded_at.strftime("%Y-%m-%d %H:00")] += record.float("estimated_cost_usd")
    return sorted(buckets.items())


def write_baseline_outputs(records: list[Record], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    task_totals = total_tokens_by_task(records)
    bar_chart(
        output_dir / "tokens_by_task.png",
        "Tokens by Task",
        "Successful calls only.",
        [task for task, _ in task_totals],
        [total for _, total in task_totals],
    )

    chat = aggregate_chat(records)
    histogram(
        output_dir / "chat_input_distribution.png",
        "Chat Input Distribution",
        "Successful chat calls only.",
        chat["inputs"],
        bins=12,
    )

    scatter_points: list[tuple[int, int]] = []
    average_points: list[tuple[int, float]] = []
    for history_messages, items in sorted(chat["by_history"].items()):
        scatter_points.extend((history_messages, item.int("input_tokens")) for item in items)
        average_points.append((history_messages, mean_or_zero([item.int("input_tokens") for item in items])))
    scatter_with_average_line(
        output_dir / "chat_input_vs_history_messages.png",
        "Chat Input vs History Messages",
        "Blue dots are calls; red line is average input by history depth.",
        scatter_points,
        average_points,
    )

    hourly_cost = cost_by_hour(records)
    line_chart(
        output_dir / "llm_cost_by_hour.png",
        "Estimated LLM Cost by Hour",
        "Successful calls with local cost estimates.",
        [label for label, _ in hourly_cost],
        [value for _, value in hourly_cost],
        color=ACCENT,
    )

    top_expensive = sorted(chat["records"], key=lambda record: record.int("total_tokens"), reverse=True)[:25]
    write_csv(
        output_dir / "top_expensive_chat_calls.csv",
        [
            "recorded_at",
            "model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "history_messages",
            "tool_use_count",
            "stop_reason",
        ],
        [
            [
                record.raw.get("recorded_at", ""),
                record.model,
                record.int("input_tokens"),
                record.int("output_tokens"),
                record.int("total_tokens"),
                record.metadata.get("history_messages", ""),
                record.metadata.get("tool_use_count", ""),
                record.raw.get("stop_reason", ""),
            ]
            for record in top_expensive
        ],
    )

    write_csv(
        output_dir / "chat_token_summary.csv",
        [
            "history_messages",
            "call_count",
            "avg_input_tokens",
            "avg_output_tokens",
            "avg_total_tokens",
            "median_input_tokens",
            "p90_input_tokens",
        ],
        [
            [
                history_messages,
                len(items),
                round(mean_or_zero([item.int("input_tokens") for item in items]), 2),
                round(mean_or_zero([item.int("output_tokens") for item in items]), 2),
                round(mean_or_zero([item.int("total_tokens") for item in items]), 2),
                statistics.median([item.int("input_tokens") for item in items]),
                p90([item.int("input_tokens") for item in items]),
            ]
            for history_messages, items in sorted(chat["by_history"].items())
        ],
    )

    summary_payload = {
        "call_count": len(records),
        "ok_call_count": sum(1 for record in records if record.status == "ok"),
        "task_totals": [{"task": task, "total_tokens": total} for task, total in task_totals],
        "chat": {
            "call_count": len(chat["records"]),
            "avg_input_tokens": round(chat["avg_input"], 2),
            "avg_output_tokens": round(chat["avg_output"], 2),
            "avg_total_tokens": round(chat["avg_total"], 2),
            "median_input_tokens": chat["median_input"],
            "p90_input_tokens": chat["p90_input"],
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")


def extract_average_prompt_component(records: list[Record], key: str) -> float:
    values: list[float] = []
    for record in records:
        if record.task != "chat" or record.status != "ok":
            continue
        value = record.metadata.get(key)
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return mean_or_zero(values)


def write_comparison_outputs(before: list[Record], after: list[Record], output_dir: Path) -> None:
    before_chat = aggregate_chat(before)
    after_chat = aggregate_chat(after)
    comparison_bars(
        output_dir / "chat_input_before_vs_after.png",
        "Chat Input Before vs After",
        "Average, median, and p90 input tokens for successful chat calls.",
        ["avg", "median", "p90"],
        [before_chat["avg_input"], before_chat["median_input"], before_chat["p90_input"]],
        [after_chat["avg_input"], after_chat["median_input"], after_chat["p90_input"]],
    )

    comparison_bars(
        output_dir / "chat_prompt_component_breakdown.png",
        "Average Prompt Component Breakdown",
        "Averages from chat metadata when present.",
        ["system", "tool schema", "memory", "user", "tool replay"],
        [
            extract_average_prompt_component(before, "system_prompt_chars"),
            extract_average_prompt_component(before, "tool_schema_chars"),
            extract_average_prompt_component(before, "retrieved_memory_chars"),
            extract_average_prompt_component(before, "user_message_chars"),
            extract_average_prompt_component(before, "tool_result_chars_total"),
        ],
        [
            extract_average_prompt_component(after, "system_prompt_chars"),
            extract_average_prompt_component(after, "tool_schema_chars"),
            extract_average_prompt_component(after, "retrieved_memory_chars"),
            extract_average_prompt_component(after, "user_message_chars"),
            extract_average_prompt_component(after, "tool_result_chars_total"),
        ],
    )

    comparison_bars(
        output_dir / "tool_result_replay_cost.png",
        "Average Tool Result Replay Cost",
        "Average replayed tool result chars per successful chat generation.",
        ["tool replay chars"],
        [extract_average_prompt_component(before, "tool_result_chars_total")],
        [extract_average_prompt_component(after, "tool_result_chars_total")],
    )

    comparison_bars(
        output_dir / "memory_injection_before_vs_after.png",
        "Memory Injection Before vs After",
        "Average selected memory count and chars from chat metadata.",
        ["memory count", "memory chars"],
        [
            extract_average_prompt_component(before, "retrieved_memory_count"),
            extract_average_prompt_component(before, "retrieved_memory_chars"),
        ],
        [
            extract_average_prompt_component(after, "retrieved_memory_count"),
            extract_average_prompt_component(after, "retrieved_memory_chars"),
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate baseline and comparison reports from llm_activity JSONL.")
    parser.add_argument(
        "--input",
        type=Path,
        help="Single llm_activity.jsonl file for a baseline report.",
    )
    parser.add_argument("--before", type=Path, help="Baseline llm_activity.jsonl file.")
    parser.add_argument("--after", type=Path, help="Post-change llm_activity.jsonl file.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("analysis/llm-baseline/latest"),
        help="Directory for charts and CSVs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.input is None and not (args.before and args.after):
        raise SystemExit("Provide either --input or both --before and --after.")

    if args.input is not None:
        records = load_records(args.input)
        write_baseline_outputs(records, args.output_dir)

    if args.before and args.after:
        before_records = load_records(args.before)
        after_records = load_records(args.after)
        write_comparison_outputs(before_records, after_records, args.output_dir)


if __name__ == "__main__":
    main()
