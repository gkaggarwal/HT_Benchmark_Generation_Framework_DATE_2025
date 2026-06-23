#!/usr/bin/env python3
"""
main.py -- command-line interface for the Compatibility-Graph-Assisted
Automatic Hardware Trojan Insertion Framework.

Examples
--------
    # combinational ISCAS-85
    python main.py netlists/c2670.v -o output/c2670 -q 8 -n 20

    # sequential ISCAS-89 scan netlist
    python main.py netlists/s1423scan.v -o output/s1423 -q 8 -n 20 \
                   --vectors 10000 --theta 0.20

    # quick smoke run
    python main.py netlists/c2670.v -o output/c2670 -q 5 -n 5 \
                   --vectors 1000 --detect 2000
"""

import argparse
import sys

from ht_framework import pipeline


def build_parser():
    p = argparse.ArgumentParser(
        description="Compatibility-graph-assisted automatic hardware trojan "
                    "insertion (DATE 2025).")
    p.add_argument('netlist', help='input gate-level Verilog netlist')
    p.add_argument('-o', '--outdir', default='output/run',
                   help='output directory')
    p.add_argument('-q', '--trigger-nodes', type=int, default=8,
                   help='number of rare trigger nodes per trojan (clique size)')
    p.add_argument('-n', '--instances', type=int, default=10,
                   help='number of HT instances to generate')
    p.add_argument('--trojan-type', choices=['combinational', 'sequential'],
                   default=None,
                   help="trojan trigger style: 'combinational' (fires the cycle "
                        "the rare condition holds) or 'sequential' (a counter/FSM "
                        "that fires only after the rare condition recurs many "
                        "times). If omitted, you are prompted.")
    p.add_argument('--counter-width', type=int, default=4,
                   help='sequential trojan counter width k: fires after '
                        '2**k - 1 occurrences of the rare condition (default 4 -> 15)')
    p.add_argument('--theta', type=float, default=0.20,
                   help='rareness threshold as a fraction of N (paper: 0.20)')
    p.add_argument('--vectors', type=int, default=10000,
                   help='number of random vectors for rare-node extraction')
    p.add_argument('--payload', default=None,
                   help='payload net name (default: first primary output)')
    p.add_argument('--detect', type=int, default=10000,
                   help='random patterns for the stealth/detection check '
                        '(0 to skip)')
    p.add_argument('--mero', action='store_true',
                   help='also run a MERO-style detection check')
    p.add_argument('--mero-n', type=int, default=2,
                   help='MERO excitation count N')
    p.add_argument('--podem-budget', type=int, default=400,
                   help='PODEM decision budget before relaxation fallback')
    p.add_argument('--max-rare', type=int, default=None,
                   help='cap on rare nodes fed to the graph (speed)')
    p.add_argument('--library', choices=['auto', 'class', 'nangate'],
                   default='auto',
                   help="source cell library: 'auto' detects, 'class' = "
                        "Synopsys/class (nnd2s1...), 'nangate' = Nangate Open "
                        "Cell Library (AND2_X1, AOI21_X1...) from Genus")
    p.add_argument('--no-visualize', action='store_true',
                   help='do not generate a trigger-subgraph image per trojan')
    p.add_argument('--viz-format', choices=['png', 'jpg'], default='png',
                   help='image format for per-trojan visualizations (default png)')
    p.add_argument('--seed', type=int, default=1, help='random seed')
    p.add_argument('--quiet', action='store_true')
    return p


def _prompt_trojan_type():
    import sys
    # Non-interactive (piped / no TTY): default to combinational without hanging.
    if not sys.stdin or not sys.stdin.isatty():
        print("[info] no --trojan-type given and stdin is non-interactive; "
              "defaulting to 'combinational'. Use --trojan-type to choose.")
        return 'combinational'
    print("\nSelect Hardware Trojan type:")
    print("  1) combinational  - fires the cycle the rare condition holds")
    print("  2) sequential     - counter/FSM; fires only after the rare "
          "condition recurs many times")
    while True:
        try:
            choice = input("Enter 1 or 2 (or 'combinational'/'sequential'): ").strip().lower()
        except EOFError:
            return 'combinational'
        if choice in ('1', 'combinational', 'comb', 'c'):
            return 'combinational'
        if choice in ('2', 'sequential', 'seq', 's'):
            return 'sequential'
        print("  Please enter 1 or 2.")


def main(argv=None):
    args = build_parser().parse_args(argv)

    trojan_type = args.trojan_type
    if trojan_type is None:
        trojan_type = _prompt_trojan_type()

    summary = pipeline.run(
        netlist_path=args.netlist,
        outdir=args.outdir,
        q=args.trigger_nodes,
        num_instances=args.instances,
        theta_fraction=args.theta,
        num_vectors=args.vectors,
        payload_signal=args.payload,
        trojan_type=trojan_type,
        counter_width=args.counter_width,
        detection_patterns=args.detect,
        run_detection=args.detect > 0,
        run_mero=args.mero,
        mero_n=args.mero_n,
        podem_budget=args.podem_budget,
        max_rare_nodes=args.max_rare,
        library=args.library,
        visualize=not args.no_visualize,
        viz_format=args.viz_format,
        seed=args.seed,
        verbose=not args.quiet,
    )
    # exit non-zero if nothing was produced
    return 0 if summary.get('instances') else 1


if __name__ == '__main__':
    sys.exit(main())
