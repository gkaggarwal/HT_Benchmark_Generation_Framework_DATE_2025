#!/usr/bin/env python3
"""
visualize_trojan.py
===================
Given an HT-infected netlist (produced by this framework) and the name of a
trojan **payload** net, extract the sub-circuit that drives that payload -- the
trojan trigger logic together with its **trigger (rare) nodes** -- and render
it as a graph.

Usage
-----
    python visualize_trojan.py infected.v                # prompts for payload
    python visualize_trojan.py infected.v --payload n570
    python visualize_trojan.py infected.v -p n570 -o my_trojan

Outputs (written next to --out, default 'trojan_subgraph'):
    <out>.dot   Graphviz source (always)
    <out>.png   rendered with Graphviz `dot` if available, else matplotlib
    <out>.svg   rendered with Graphviz `dot` if available

The trigger nodes (the circuit's rare nodes feeding the trojan) are highlighted
in green; the payload net is red; the original payload source is orange;
trigger-logic gates are blue; counter/FSM flip-flops (sequential trojans) are
gold.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys


# ---------------------------------------------------------------------------
# Netlist parsing (simplified primitive format)
# ---------------------------------------------------------------------------

def parse_netlist(path):
    text = open(path).read()
    text = re.sub(r'//[^\n]*', ' ', text)
    inputs, outputs = [], []
    m = re.search(r'\binput\s+([^;]+);', text)
    if m:
        inputs = [s.strip() for s in m.group(1).split(',') if s.strip()]
    m = re.search(r'\boutput\s+([^;]+);', text)
    if m:
        outputs = [s.strip() for s in m.group(1).split(',') if s.strip()]

    gates = []          # {type, inst, outs:[...], ins:[...]}
    driver_of = {}      # net -> gate
    for m in re.finditer(r'(\w+)\s+([\w\\/\[\]]+)\s*\(([^;]+)\)\s*;', text):
        gtype, inst, body = m.group(1), m.group(2), m.group(3)
        if gtype.lower() in ('module', 'input', 'output', 'wire', 'assign',
                             'endmodule'):
            continue
        pins = [p.strip() for p in body.split(',')]
        gtype_l = gtype.lower()
        ncout = 2 if gtype_l in ('sdff', 'sdffr') else 1
        g = {'type': gtype_l, 'inst': inst,
             'outs': pins[:ncout], 'ins': pins[ncout:]}
        gates.append(g)
        for o in g['outs']:
            driver_of[o] = g
    return {'inputs': inputs, 'outputs': outputs,
            'gates': gates, 'driver_of': driver_of}


def is_trojan(name):
    """A net or instance belonging to inserted trojan logic."""
    return 'troj' in name.lower()


# ---------------------------------------------------------------------------
# Subgraph extraction
# ---------------------------------------------------------------------------

def extract_trigger_subgraph(net, payload):
    """Return a dict describing the trojan sub-circuit driving `payload`.

    Keys: payload, payload_source, fire_signal, trojan_type,
          gates (list), trigger_nodes (set), edges (list of (src,dst)),
          ff_nets (set of flip-flop output nets).
    """
    driver_of = net['driver_of']
    if payload not in driver_of:
        raise SystemExit(
            f"Payload net '{payload}' is not driven by any gate in this "
            f"netlist.\nAvailable trojan payload nets (XOR_*payload): "
            f"{', '.join(_list_payloads(net)) or '(none found)'}")

    pay_gate = driver_of[payload]
    # the payload driver should be the trojan XOR (payload = source XOR fire)
    troj_inputs = [i for i in pay_gate['ins'] if is_trojan(i)]
    src_inputs = [i for i in pay_gate['ins'] if not is_trojan(i)]
    fire_signal = troj_inputs[0] if troj_inputs else None
    payload_source = src_inputs[0] if src_inputs else None
    if fire_signal is None:
        raise SystemExit(
            f"Net '{payload}' does not look like a trojan payload "
            f"(its driver has no trojan input). Pick a payload net such as "
            f"{', '.join(_list_payloads(net)) or '(none found)'}")

    gates = []          # trojan gates included in the subgraph
    seen_gate = set()
    trigger_nodes = set()
    ff_nets = set()
    edges = []          # (src_net, dst_net) data-flow edges
    edge_seen = set()

    def add_edge(s, d):
        if (s, d) not in edge_seen:
            edge_seen.add((s, d))
            edges.append((s, d))

    # walk backward from the payload gate over trojan logic only
    stack = [payload]
    visited = set()
    while stack:
        out = stack.pop()
        if out in visited:
            continue
        visited.add(out)
        g = driver_of.get(out)
        if g is None:
            continue
        if id(g) not in seen_gate:
            seen_gate.add(id(g))
            gates.append(g)
        if g['type'] in ('sdff', 'sdffr'):
            ff_nets.update(g['outs'])
        # which inputs to follow / classify
        is_payload_xor = (out == payload)
        # data inputs (skip scan input of flops to reduce clutter)
        data_ins = g['ins']
        if g['type'] in ('sdff', 'sdffr'):
            # sdff(q, qbar, data, scan): keep data, drop scan
            data_ins = g['ins'][:1]
        for inp in data_ins:
            if is_payload_xor and inp == payload_source:
                add_edge(inp, out)          # original signal into payload XOR
                continue
            if is_trojan(inp) and inp in driver_of:
                add_edge(inp, out)
                stack.append(inp)
            else:
                # boundary: a circuit net feeding the trojan -> trigger node
                trigger_nodes.add(inp)
                add_edge(inp, out)

    trojan_type = 'sequential' if ff_nets else 'combinational'
    return {
        'payload': payload,
        'payload_source': payload_source,
        'fire_signal': fire_signal,
        'trojan_type': trojan_type,
        'gates': gates,
        'trigger_nodes': trigger_nodes,
        'ff_nets': ff_nets,
        'edges': edges,
    }


def _list_payloads(net):
    pays = []
    for g in net['gates']:
        if 'payload' in g['inst'].lower() and g['outs']:
            pays.append(g['outs'][0])
    return pays


# ---------------------------------------------------------------------------
# Graphviz DOT generation
# ---------------------------------------------------------------------------

def to_dot(sub):
    payload = sub['payload']
    src = sub['payload_source']
    triggers = sub['trigger_nodes']
    ff = sub['ff_nets']
    driver_label = {}
    for g in sub['gates']:
        for o in g['outs']:
            driver_label[o] = g

    def node_attr(netname):
        if netname == payload:
            return ('"%s"' % netname,
                    'shape=box, style="filled,bold", fillcolor="#ff6b6b", '
                    'fontcolor=white, label="PAYLOAD\\n%s"' % netname)
        if netname == src:
            return ('"%s"' % netname,
                    'shape=box, style=filled, fillcolor="#ffa94d", '
                    'label="payload source\\n%s"' % netname)
        if netname in triggers:
            return ('"%s"' % netname,
                    'shape=box, style="filled,bold", fillcolor="#51cf66", '
                    'label="trigger node\\n%s"' % netname)
        g = driver_label.get(netname)
        if g is not None:
            if g['type'] in ('sdff', 'sdffr'):
                return ('"%s"' % netname,
                        'shape=box, style=filled, fillcolor="#ffd43b", '
                        'label="DFF\\n%s"' % netname)
            return ('"%s"' % netname,
                    'shape=ellipse, style=filled, fillcolor="#74c0fc", '
                    'label="%s\\n%s"' % (g['type'].upper(), netname))
        return ('"%s"' % netname, 'shape=ellipse, label="%s"' % netname)

    lines = ['digraph trojan {', '  rankdir=LR;', '  node [fontsize=10];',
             '  edge [color="#868e96"];']
    nodes = set([payload])
    if src:
        nodes.add(src)
    nodes |= triggers
    for s, d in sub['edges']:
        nodes.add(s); nodes.add(d)
    for n in nodes:
        nid, attr = node_attr(n)
        lines.append(f'  {nid} [{attr}];')
    for s, d in sub['edges']:
        lines.append(f'  "{s}" -> "{d}";')
    # rank trigger nodes together on the left
    if triggers:
        same = ' '.join('"%s"' % t for t in triggers)
        lines.append('  { rank=source; %s }' % same)
    lines.append(f'  {{ rank=sink; "{payload}" }}')
    lines.append('}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(dot_text, out_base, fmt='png'):
    """Produce ONLY the raster image (<out_base>.<fmt>); no .dot or .svg files.

    Order of preference: Graphviz `dot` (best quality, fed via stdin so no .dot
    file is written) -> networkx+matplotlib with a self-contained layered layout
    (no pygraphviz needed). Returns the list of files written.
    """
    fmt = fmt.lower()
    if fmt == 'jpeg':
        fmt = 'jpg'
    if fmt == 'svg':                      # svg is vector-only; keep it usable
        fmt = 'png'
    made = []
    img = f'{out_base}.{fmt}'

    dot_bin = shutil.which('dot')
    if dot_bin:
        try:
            subprocess.run([dot_bin, f'-T{fmt}', '-o', img],
                           input=dot_text.encode(), check=True,
                           stderr=subprocess.DEVNULL)
            made.append(img)
        except Exception:
            pass

    if img not in made:                   # graphviz absent or failed
        produced = _render_matplotlib(dot_text, out_base, fmt)
        if produced:
            made.append(produced)
    return made


def _layered_positions(G):
    """A left-to-right layered layout (longest-path layering) that needs no
    external layout engine and tolerates the counter feedback cycles."""
    import networkx as nx
    # longest-path layer via relaxation, ignoring back-edges (cycle-safe)
    order = list(G.nodes())
    try:
        order = list(nx.topological_sort(G))
        acyclic = True
    except Exception:
        acyclic = False
    layer = {n: 0 for n in G.nodes()}
    if acyclic:
        for n in order:
            for s in G.successors(n):
                layer[s] = max(layer[s], layer[n] + 1)
    else:
        # relaxation with a cap; back-edges simply don't push further
        for _ in range(len(G)):
            changed = False
            for u, v in G.edges():
                if layer[v] < layer[u] + 1:
                    layer[v] = layer[u] + 1
                    changed = True
            if not changed:
                break
    # group by layer, assign y within layer
    from collections import defaultdict
    cols = defaultdict(list)
    for n in G.nodes():
        cols[layer[n]].append(n)
    pos = {}
    for x, nodes in cols.items():
        nodes.sort()
        h = len(nodes)
        for i, n in enumerate(nodes):
            pos[n] = (x * 2.2, (h - 1) / 2.0 - i)
    return pos


def _render_matplotlib(dot_text, out_base, fmt='png'):
    """Fallback rendering with networkx + matplotlib (no Graphviz needed)."""
    try:
        import networkx as nx
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except Exception:
        return None

    G = nx.DiGraph()
    for m in re.finditer(r'"([^"]+)"\s*\[([^\]]+)\]', dot_text):
        name, attr = m.group(1), m.group(2)
        color = '#74c0fc'
        for hexc in ('ff6b6b', 'ffa94d', '51cf66', 'ffd43b'):
            if hexc in attr:
                color = '#' + hexc
        lm = re.search(r'label="([^"]+)"', attr)
        label = lm.group(1).replace('\\n', '\n') if lm else name
        G.add_node(name, color=color, label=label)
    for m in re.finditer(r'"([^"]+)"\s*->\s*"([^"]+)"', dot_text):
        G.add_edge(m.group(1), m.group(2))
    if G.number_of_nodes() == 0:
        return None

    # prefer graphviz layout if pygraphviz/pydot exist; else built-in layered
    pos = None
    for prog in ('nx_agraph', 'nx_pydot'):
        try:
            pos = getattr(nx, prog).graphviz_layout(G, prog='dot')
            break
        except Exception:
            pos = None
    if pos is None:
        pos = _layered_positions(G)

    ncols = max(1, len({round(p[0], 3) for p in pos.values()}))
    nrows = max(1, len(G.nodes()) // max(1, ncols) + 1)
    plt.figure(figsize=(max(9, ncols * 2.6), max(5, nrows * 1.3)))
    nx.draw_networkx_edges(G, pos, arrows=True, edge_color='#adb5bd',
                           node_size=2200, arrowsize=14)
    nx.draw_networkx_nodes(G, pos,
                           node_color=[G.nodes[n]['color'] for n in G.nodes],
                           node_size=2200, edgecolors='#343a40')
    nx.draw_networkx_labels(G, pos,
                            labels={n: G.nodes[n]['label'] for n in G.nodes},
                            font_size=7)
    plt.axis('off')
    plt.tight_layout()
    out = f'{out_base}.{fmt}'
    try:
        plt.savefig(out, dpi=150, bbox_inches='tight')
    except (ValueError, OSError):
        # e.g. jpg requested but Pillow missing -> fall back to png
        out = out_base + '.png'
        plt.savefig(out, dpi=150, bbox_inches='tight')
    plt.close()
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def prompt_payload(net):
    pays = _list_payloads(net)
    print("\nHT-infected netlist loaded.")
    if pays:
        print("Detected trojan payload net(s):", ', '.join(pays))
    if not sys.stdin or not sys.stdin.isatty():
        if pays:
            print(f"[info] non-interactive; using first payload '{pays[0]}'.")
            return pays[0]
        raise SystemExit("Provide --payload (no TTY to prompt).")
    while True:
        ans = input("Enter the trojan payload net name: ").strip()
        if ans:
            return ans


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Visualize the trigger subgraph of a trojan payload.")
    ap.add_argument('netlist', help='HT-infected netlist (.v)')
    ap.add_argument('-p', '--payload', default=None,
                    help='trojan payload net name (prompted if omitted)')
    ap.add_argument('-o', '--out', default='trojan_subgraph',
                    help='output basename for the image')
    ap.add_argument('-f', '--format', default='png',
                    choices=['png', 'jpg'],
                    help='image format to generate (default png)')
    args = ap.parse_args(argv)

    net = parse_netlist(args.netlist)
    payload = args.payload or prompt_payload(net)
    sub = extract_trigger_subgraph(net, payload)

    # textual summary
    print(f"\nTrojan type     : {sub['trojan_type']}")
    print(f"Payload net     : {sub['payload']}")
    print(f"Payload source  : {sub['payload_source']}")
    print(f"Activation (fire): {sub['fire_signal']}")
    print(f"Trigger nodes ({len(sub['trigger_nodes'])}): "
          f"{', '.join(sorted(sub['trigger_nodes']))}")
    print(f"Trojan gates    : {len(sub['gates'])}"
          + (f"  (incl. {len(sub['ff_nets'])} flip-flop bits)"
             if sub['ff_nets'] else ""))

    made = render(to_dot(sub), args.out, fmt=args.format)
    if made:
        print("\nWrote:")
        for f in made:
            print("  ", f)
    else:
        print("\n  [!] Could not produce an image: neither Graphviz `dot` nor "
              "matplotlib is available.\n      Install one of them, e.g.:\n"
              "        pip install matplotlib networkx      # pure-Python")


if __name__ == '__main__':
    main()


# ---------------------------------------------------------------------------
# Programmatic entry point (used by the pipeline)
# ---------------------------------------------------------------------------

def generate_image(infected_file, payload, out_base, fmt='png'):
    """Parse an infected netlist, extract the trigger subgraph for `payload`,
    and render it to <out_base>.<fmt> (+ .dot).  Returns (files, summary)."""
    net = parse_netlist(infected_file)
    sub = extract_trigger_subgraph(net, payload)
    files = render(to_dot(sub), out_base, fmt=fmt)
    summary = {
        'trojan_type': sub['trojan_type'],
        'payload': sub['payload'],
        'fire_signal': sub['fire_signal'],
        'trigger_nodes': sorted(sub['trigger_nodes']),
        'num_trojan_gates': len(sub['gates']),
        'image': next((f for f in files
                       if f.endswith(('.png', '.jpg', '.svg', '.pdf'))), None),
        'files': files,
    }
    return files, summary
