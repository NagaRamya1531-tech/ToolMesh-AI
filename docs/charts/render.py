"""Render README figures from committed ToolMesh benchmark exports."""

import gzip
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

OUT = Path(__file__).resolve().parent
RESULTS = OUT.parents[1] / "benchmarks" / "results"
COLORS = {
    "full": "#64748b",
    "keyword": "#6366f1",
    "vector": "#0788ac",
    "hybrid": "#047857",
}


def render(filename, title, subtitle, items, *, maximum, percent=False, target=None):
    plt.rcParams.update(
        {
            "svg.fonttype": "path",
            "svg.hashsalt": "toolmesh",
            "font.family": "DejaVu Sans",
        }
    )
    fig, axes = plt.subplots(figsize=(8.6, 3.0))
    fig.patch.set_facecolor("#f8fafc")
    axes.set_facecolor("#f8fafc")
    fig.subplots_adjust(left=0.23, right=0.91, top=0.70, bottom=0.20)
    fig.text(0.045, 0.90, title, fontsize=17, color="#0f172a", weight="bold")
    fig.text(0.045, 0.79, subtitle, fontsize=10, color="#475569")
    for row, (strategy, label, value) in enumerate(items):
        axes.barh(row, value, height=0.54, color=COLORS[strategy], zorder=3)
        text = f"{value * 100:.1f}%" if percent else f"{value:,.0f}"
        axes.text(
            (1.01 if percent else value + maximum * 0.018),
            row,
            text,
            va="center",
            color="#0f172a",
            weight="bold",
        )
    axes.set_yticks(range(len(items)), [label for _, label, _ in items])
    axes.set_xlim(0, maximum)
    axes.invert_yaxis()
    axes.grid(axis="x", color="#dce4ef")
    axes.tick_params(length=0, pad=7, labelcolor="#334155")
    for spine in axes.spines.values():
        spine.set_visible(False)
    if percent:
        axes.set_xticks([0, 0.25, 0.50, 0.75, 1])
        axes.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0%}"))
    else:
        axes.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}"))
    if target is not None:
        axes.axvline(target, color="#64748b", ls="--", lw=1, zorder=4)
        fig.text(
            0.67,
            0.05,
            f"Dashed line · {target:.0%} target",
            color="#475569",
            fontsize=9,
        )
    description = "; ".join(
        f"{label}: {value:.1%}" if percent else f"{label}: {value:,}"
        for _, label, value in items
    )
    fig.savefig(
        OUT / filename,
        metadata={"Date": None, "Title": title, "Description": description},
    )
    plt.close(fig)
    output = OUT / filename
    output.write_text(
        "\n".join(line.rstrip() for line in output.read_text().splitlines()) + "\n"
    )


def main():
    with gzip.open(RESULTS / "m4-seed42-10000x1000.json.gz", "rt") as file:
        run = json.load(file)["run"]
    strategies = ("keyword", "vector", "hybrid")
    scores = run["summary"]["strategies"]
    render(
        "retrieval-recall.svg",
        "Retrieval quality by ranking strategy",
        f"{run['catalog_size']:,} synthetic definitions · {run['case_count']:,} held-out queries · Recall@5",
        [(key, key.title(), scores[key]["recall_at_5"]) for key in strategies],
        maximum=1.1,
        percent=True,
        target=0.95,
    )
    report = json.loads((RESULTS / "m8-openai-corrected.json").read_text())
    grouped = defaultdict(list)
    for trial in report["provider_trials"]:
        grouped[trial["strategy"]].append(trial)
    totals = []
    for strategy in ("full", *strategies):
        rows = grouped[strategy]
        label = "Upfront schemas" if strategy == "full" else strategy.title()
        totals.append(
            (strategy, label, sum(row["usage"]["input_tokens"] for row in rows))
        )
    verified = sum(row["success"] for row in report["provider_trials"])
    render(
        "provider-input-tokens.svg",
        "Input tokens per strategy",
        f"GPT-5.4 mini · {verified}/{len(report['provider_trials'])} verified sandbox reads · Four trials per strategy",
        totals,
        maximum=max(item[2] for item in totals) * 1.16,
    )


if __name__ == "__main__":
    main()
