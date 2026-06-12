#!/usr/bin/env python3
"""Generate paper-quality figures from split vs baseline experiment data.

Usage:
    python scripts/generate_figures.py \
        --baseline data/baseline_24h.csv \
        --split results/split_report.csv \
        --output figures/

Figure types:
    1. Coverage growth over time (split vs baseline, per benchmark)
    2. Bug discovery over time (split vs baseline, per benchmark)
    3. Variance comparison (split vs baseline)
    4. Branch tree visualization (split structure)
    5. Summary table (all benchmarks × all fuzzers)
    6. Sparsity split timing (when splits happen)
    7. Estimator convergence (D_R(T) over time)
    8. Dashboard (combined view per benchmark)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd
import seaborn as sns

# Style
sns.set_style("whitegrid")
plt.rcParams.update({
    'font.size': 12,
    'axes.titlesize': 14,
    'axes.labelsize': 12,
    'legend.fontsize': 10,
    'figure.figsize': (10, 6),
    'savefig.dpi': 300,
    'savefig.bbox': 'tight',
})

FUZZER_COLORS = {
    'afl': '#1f77b4',
    'aflfast': '#ff7f0e',
    'aflplusplus': '#2ca02c',
    'aflsmart': '#d62728',
    'entropic': '#9467bd',
    'fairfuzz': '#8c564b',
    'honggfuzz': '#e377c2',
    'libfuzzer': '#7f7f7f',
    'mopt': '#bcbd22',
}


def load_data(path: str) -> pd.DataFrame:
    """Load CSV (supports .csv and .csv.gz)."""
    if path.endswith('.gz'):
        return pd.read_csv(path, compression='gzip')
    return pd.read_csv(path)


def forward_fill_coverage(df: pd.DataFrame, time_col='time',
                          value_col='edges_covered') -> pd.DataFrame:
    """Forward-fill coverage values (FuzzBench style)."""
    df = df.sort_values(time_col)
    df[value_col] = df[value_col].ffill()
    return df


# ---- Figure 1: Coverage Growth ----
def plot_coverage_growth(df: pd.DataFrame, benchmark: str, output_dir: Path,
                         label_prefix: str = '', title_suffix: str = ''):
    """Coverage over time, one line per fuzzer (median across trials)."""
    bm_df = df[df['benchmark'] == benchmark].copy()
    if bm_df.empty:
        print(f"  No data for {benchmark}, skipping coverage plot")
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    for fuzzer in sorted(bm_df['fuzzer'].unique()):
        fdf = bm_df[bm_df['fuzzer'] == fuzzer]
        # Group by time, take median across trials
        agg = fdf.groupby('time')['edges_covered'].median().reset_index()
        agg = agg.sort_values('time')
        color = FUZZER_COLORS.get(fuzzer, None)
        ax.plot(agg['time'] / 3600, agg['edges_covered'],
                label=fuzzer, color=color, linewidth=1.5)

    ax.set_xlabel('Time (hours)')
    ax.set_ylabel('Edges Covered')
    ax.set_title(f'Coverage Growth — {benchmark}{title_suffix}')
    ax.legend(loc='lower right', ncol=2)
    ax.grid(True, alpha=0.3)

    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'{label_prefix}coverage_{benchmark}.{fmt}')
    plt.close(fig)


# ---- Figure 2: Bug Discovery ----
def plot_bug_discovery(df: pd.DataFrame, benchmark: str, output_dir: Path,
                       label_prefix: str = '', title_suffix: str = ''):
    """Cumulative bugs over time, one line per fuzzer."""
    bm_df = df[df['benchmark'] == benchmark].copy()
    if bm_df.empty:
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    for fuzzer in sorted(bm_df['fuzzer'].unique()):
        fdf = bm_df[bm_df['fuzzer'] == fuzzer]
        if 'bugs_covered' in fdf.columns:
            agg = fdf.groupby('time')['bugs_covered'].median().reset_index()
            agg = agg.sort_values('time')
            color = FUZZER_COLORS.get(fuzzer, None)
            ax.plot(agg['time'] / 3600, agg['bugs_covered'],
                    label=fuzzer, color=color, linewidth=1.5)

    ax.set_xlabel('Time (hours)')
    ax.set_ylabel('Bugs Found')
    ax.set_title(f'Bug Discovery — {benchmark}{title_suffix}')
    ax.legend(loc='lower right', ncol=2)
    ax.grid(True, alpha=0.3)

    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'{label_prefix}bugs_{benchmark}.{fmt}')
    plt.close(fig)


# ---- Figure 3: Variance Comparison ----
def plot_variance_comparison(baseline_df: pd.DataFrame, split_df: pd.DataFrame,
                             benchmark: str, output_dir: Path):
    """Box plots comparing coverage variance: baseline vs split."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    for ax, (df, label) in zip(axes, [(baseline_df, 'Baseline'), (split_df, 'Split')]):
        bm_df = df[df['benchmark'] == benchmark]
        if bm_df.empty:
            ax.set_title(f'{label} — {benchmark} (no data)')
            continue

        # Get final coverage per trial
        final = bm_df.groupby(['fuzzer', 'trial_id'])['edges_covered'].max().reset_index()
        if not final.empty:
            pal = {f: FUZZER_COLORS.get(f, '#333333') for f in final['fuzzer'].unique()}
            sns.boxplot(data=final, x='fuzzer', y='edges_covered', ax=ax, palette=pal)
            ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha='right')
        ax.set_title(f'{label} — {benchmark}')
        ax.set_ylabel('Final Coverage')

    fig.suptitle(f'Coverage Variance — {benchmark}', fontsize=14)
    plt.tight_layout()
    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'variance_{benchmark}.{fmt}')
    plt.close(fig)


# ---- Figure 4: Split Timing ----
def plot_split_timing(split_df: pd.DataFrame, benchmark: str, output_dir: Path):
    """Show when splits happen and how branches evolve."""
    bm_df = split_df[split_df['benchmark'] == benchmark]
    if bm_df.empty or 'split_level' not in bm_df.columns:
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    levels = sorted(bm_df['split_level'].unique())
    for level in levels:
        ldf = bm_df[bm_df['split_level'] == level]
        branches = ldf['branch_id'].unique()
        ax.barh(level, len(branches), height=0.5, alpha=0.7,
                label=f'Level {level}: {len(branches)} branches')

    ax.set_xlabel('Number of Branches')
    ax.set_ylabel('Split Level')
    ax.set_title(f'Split Structure — {benchmark}')
    ax.legend()
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'split_timing_{benchmark}.{fmt}')
    plt.close(fig)


# ---- Figure 5: Summary Table ----
def plot_summary_table(baseline_df: pd.DataFrame, split_df: Optional[pd.DataFrame],
                       output_dir: Path):
    """Summary heatmap: final coverage per (benchmark, fuzzer)."""
    # Baseline final coverage
    if baseline_df.empty:
        return

    final = baseline_df.groupby(['benchmark', 'fuzzer'])['edges_covered'].max().reset_index()
    pivot = final.pivot(index='benchmark', columns='fuzzer', values='edges_covered')

    fig, ax = plt.subplots(figsize=(12, max(6, len(pivot) * 0.5)))
    sns.heatmap(pivot, annot=True, fmt='.0f', cmap='YlGnBu', ax=ax,
                linewidths=0.5, cbar_kws={'label': 'Max Edges Covered'})
    ax.set_title('Final Coverage (Baseline)')
    plt.tight_layout()

    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'summary_table.{fmt}')
    plt.close(fig)


# ---- Figure 6: Estimator Convergence ----
def plot_estimator_convergence(df: pd.DataFrame, benchmark: str, output_dir: Path):
    """Plot D_R(T) or bug detection ratio over time."""
    bm_df = df[df['benchmark'] == benchmark]
    if bm_df.empty or 'bugs_covered' not in bm_df.columns:
        return

    fig, ax = plt.subplots(figsize=(10, 6))

    for fuzzer in sorted(bm_df['fuzzer'].unique()):
        fdf = bm_df[bm_df['fuzzer'] == fuzzer].sort_values('time')
        if fdf.empty:
            continue
        # Bug detection ratio: cumulative bugs / total executions proxy
        times = fdf.groupby('time')['bugs_covered'].median().reset_index()
        times = times.sort_values('time')
        if len(times) > 1 and times['bugs_covered'].max() > 0:
            ratio = times['bugs_covered'] / (times.index + 1)
            color = FUZZER_COLORS.get(fuzzer, None)
            ax.plot(times['time'] / 3600, ratio,
                    label=fuzzer, color=color, linewidth=1.5)

    ax.set_xlabel('Time (hours)')
    ax.set_ylabel('Bug Detection Ratio')
    ax.set_title(f'Bug Detection Ratio — {benchmark}')
    ax.legend(loc='upper right', ncol=2)
    ax.grid(True, alpha=0.3)

    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'estimator_{benchmark}.{fmt}')
    plt.close(fig)


# ---- Figure 7: Dashboard ----
def plot_dashboard(df: pd.DataFrame, benchmark: str, output_dir: Path,
                   label_prefix: str = ''):
    """Combined 2x2 dashboard: coverage, bugs, ratio, variance."""
    bm_df = df[df['benchmark'] == benchmark]
    if bm_df.empty:
        return

    fig, axes = plt.subplots(2, 2, figsize=(16, 12))

    # Coverage growth
    ax = axes[0, 0]
    for fuzzer in sorted(bm_df['fuzzer'].unique()):
        fdf = bm_df[bm_df['fuzzer'] == fuzzer]
        agg = fdf.groupby('time')['edges_covered'].median().reset_index().sort_values('time')
        ax.plot(agg['time'] / 3600, agg['edges_covered'],
                label=fuzzer, color=FUZZER_COLORS.get(fuzzer), linewidth=1.2)
    ax.set_title('Coverage Growth')
    ax.set_xlabel('Time (hours)')
    ax.set_ylabel('Edges')
    ax.legend(fontsize=8, ncol=2)

    # Bug discovery
    ax = axes[0, 1]
    if 'bugs_covered' in bm_df.columns:
        for fuzzer in sorted(bm_df['fuzzer'].unique()):
            fdf = bm_df[bm_df['fuzzer'] == fuzzer]
            agg = fdf.groupby('time')['bugs_covered'].median().reset_index().sort_values('time')
            ax.plot(agg['time'] / 3600, agg['bugs_covered'],
                    label=fuzzer, color=FUZZER_COLORS.get(fuzzer), linewidth=1.2)
    ax.set_title('Bug Discovery')
    ax.set_xlabel('Time (hours)')
    ax.set_ylabel('Bugs')

    # Final coverage box plot
    ax = axes[1, 0]
    final = bm_df.groupby(['fuzzer', 'trial_id'])['edges_covered'].max().reset_index()
    if not final.empty:
        pal = {f: FUZZER_COLORS.get(f, '#333333') for f in final['fuzzer'].unique()}
        sns.boxplot(data=final, x='fuzzer', y='edges_covered', ax=ax, palette=pal)
        ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha='right', fontsize=8)
    ax.set_title('Coverage Variance')

    # Bug detection ratio
    ax = axes[1, 1]
    if 'bugs_covered' in bm_df.columns:
        for fuzzer in sorted(bm_df['fuzzer'].unique()):
            fdf = bm_df[bm_df['fuzzer'] == fuzzer].sort_values('time')
            times = fdf.groupby('time')['bugs_covered'].median().reset_index().sort_values('time')
            if len(times) > 1 and times['bugs_covered'].max() > 0:
                ratio = times['bugs_covered'] / (times.index + 1)
                ax.plot(times['time'] / 3600, ratio,
                        label=fuzzer, color=FUZZER_COLORS.get(fuzzer), linewidth=1.2)
    ax.set_title('Bug Detection Ratio')
    ax.set_xlabel('Time (hours)')

    fig.suptitle(f'{benchmark}', fontsize=16, fontweight='bold')
    plt.tight_layout()
    for fmt in ['png', 'pdf']:
        fig.savefig(output_dir / f'{label_prefix}dashboard_{benchmark}.{fmt}')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description='Generate paper-quality figures')
    parser.add_argument('--baseline', required=True, help='Baseline CSV (no-split)')
    parser.add_argument('--split', default=None, help='Split experiment CSV (optional)')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--benchmarks', nargs='*', default=None,
                        help='Specific benchmarks to plot (default: all)')
    parser.add_argument('--figure-types', nargs='*',
                        default=['coverage', 'bugs', 'dashboard', 'summary', 'estimator'],
                        help='Figure types to generate')
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading baseline: {args.baseline}")
    baseline_df = load_data(args.baseline)
    print(f"  {len(baseline_df)} rows, benchmarks: {baseline_df['benchmark'].nunique()}")

    split_df = None
    if args.split:
        print(f"Loading split: {args.split}")
        split_df = load_data(args.split)
        print(f"  {len(split_df)} rows")

    benchmarks = args.benchmarks or sorted(baseline_df['benchmark'].unique())
    print(f"Generating figures for {len(benchmarks)} benchmarks...")

    for bm in benchmarks:
        print(f"  {bm}...")
        if 'coverage' in args.figure_types:
            plot_coverage_growth(baseline_df, bm, output_dir, label_prefix='baseline_')
            if split_df is not None:
                plot_coverage_growth(split_df, bm, output_dir,
                                    label_prefix='split_', title_suffix=' (Split)')
        if 'bugs' in args.figure_types:
            plot_bug_discovery(baseline_df, bm, output_dir, label_prefix='baseline_')
            if split_df is not None:
                plot_bug_discovery(split_df, bm, output_dir,
                                  label_prefix='split_', title_suffix=' (Split)')
        if 'variance' in args.figure_types and split_df is not None:
            plot_variance_comparison(baseline_df, split_df, bm, output_dir)
        if 'split_timing' in args.figure_types and split_df is not None:
            plot_split_timing(split_df, bm, output_dir)
        if 'estimator' in args.figure_types:
            plot_estimator_convergence(baseline_df, bm, output_dir)
        if 'dashboard' in args.figure_types:
            plot_dashboard(baseline_df, bm, output_dir, label_prefix='baseline_')
            if split_df is not None:
                plot_dashboard(split_df, bm, output_dir, label_prefix='split_')

    if 'summary' in args.figure_types:
        print("  Summary table...")
        plot_summary_table(baseline_df, split_df, output_dir)

    print(f"Done. Figures saved to {output_dir}/")


if __name__ == '__main__':
    main()
