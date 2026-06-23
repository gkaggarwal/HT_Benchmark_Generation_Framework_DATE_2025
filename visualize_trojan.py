#!/usr/bin/env python3
"""
visualize_trojan.py  --  CLI wrapper.

Given an HT-infected netlist and a trojan payload net, extract the sub-circuit
that drives that payload (the trojan trigger logic + its trigger/rare nodes)
and render it as a .png/.jpg/.svg image (plus Graphviz .dot).

    python visualize_trojan.py infected.v                 # prompts for payload
    python visualize_trojan.py infected.v -p n570 -o my_trojan -f png

All logic lives in ht_framework/trojan_visualize.py so the same code is reused
by the pipeline (which auto-generates one image per inserted trojan).
"""
from ht_framework.trojan_visualize import main

if __name__ == '__main__':
    main()
