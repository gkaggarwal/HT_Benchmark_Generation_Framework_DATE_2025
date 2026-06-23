"""
rare_nodes.py
=============
Algorithm 1 of the paper: extraction of rare nodes by functional simulation.

A random vector set V is applied to the (combinational view of the) circuit.
For every internal node we count how often it takes logic 0 and logic 1.
A node whose 1-count is below the threshold is a *rare-1* node (rare value 1);
a node whose 0-count is below the threshold is a *rare-0* node (rare value 0).

The threshold is expressed as a fraction of N (the paper selects 20 %).
"""

import random
from . import netlist_simulation


def extract_rare_nodes(netlist_file, theta_fraction=0.20, N=10000,
                       seed=None, verbose=False, exclude_primary_io=True):
    """Run Algorithm 1.

    Parameters
    ----------
    netlist_file : path to a *simplified* netlist.
    theta_fraction : rareness threshold as a fraction of N (paper: 0.20).
    N : number of random vectors.
    seed : RNG seed for reproducibility.
    exclude_primary_io : if True, primary inputs and outputs are not eligible
        as trigger (rare) nodes -- triggers are taken from internal nodes only,
        matching the paper's intent of hidden trigger logic.

    Returns
    -------
    dict with keys:
        'rare1'      : list of rare-value-1 node names
        'rare0'      : list of rare-value-0 node names
        'rare_value' : {node: 0/1}
        'net_counts' : {node: {'0':c0,'1':c1}}
        'total_nodes': int
        'threshold'  : absolute count threshold used
    """
    if seed is not None:
        random.seed(seed)

    sim = netlist_simulation.ISCAS85Simulator(netlist_file, VERBOSE=verbose)
    input_length = len(sim.inputs)
    state_length = len(sim.flip_flops)
    threshold = max(1, int(round(theta_fraction * N)))

    net_counts = {}
    seed_vectors = {}   # node -> (input_vec_str, state_vec_str) first hit at rare value
    # we record a seed only once the node is confirmed rare (post-hoc), so we
    # keep a small rolling record of the most recent hit per (node,value).
    last_hit = {}       # (node, value) -> (ivec, svec)
    for i in range(N):
        ivec = ''.join(random.choice('01') for _ in range(input_length))
        svec = ''.join(random.choice('01') for _ in range(state_length))
        sim.set_inputs_from_binary(ivec)
        sim.set_flip_flop_state_from_binary(svec)
        value_wire, _, _ = sim.simulate()
        for net, val in value_wire.items():
            c = net_counts.get(net)
            if c is None:
                c = {'0': 0, '1': 0}
                net_counts[net] = c
            c[str(val)] += 1
            last_hit[(net, val)] = (ivec, svec)

    primary_io = set(sim.inputs) | set(sim.outputs) if exclude_primary_io else set()
    # flip-flop Q / QBAR are pseudo inputs - they are legitimate internal
    # trigger candidates, so they are NOT excluded.

    rare1, rare0, rare_value = [], [], {}
    for net, c in net_counts.items():
        if net in primary_io:
            continue
        # A node is rare-1 if it reaches 1 only rarely (its rare value is 1)
        if 0 < c['1'] < threshold:
            rare1.append(net)
            rare_value[net] = 1
        elif 0 < c['0'] < threshold:
            rare0.append(net)
            rare_value[net] = 0

    # Build a named seed assignment {input_name: 0/1, ff_q: 0/1} for each rare
    # node, recorded from the last random vector that hit its rare value.  This
    # is a guaranteed-satisfying full assignment used for fast care-bit
    # relaxation downstream.
    input_names = list(sim.inputs)
    ff_q_names = [ff['q'] for ff in sim.flip_flops.values()]
    seed_assign = {}
    for net in list(rare_value.keys()):
        rv = rare_value[net]
        hit = last_hit.get((net, rv))
        if hit is None:
            continue
        ivec, svec = hit
        a = {input_names[k]: int(ivec[k]) for k in range(len(input_names))}
        for k, qn in enumerate(ff_q_names):
            a[qn] = int(svec[k])
        seed_assign[net] = a

    if verbose:
        print(f"[rare_nodes] N={N}, threshold={threshold} "
              f"({theta_fraction*100:.0f}% of N)")
        print(f"[rare_nodes] rare-1 nodes : {len(rare1)}")
        print(f"[rare_nodes] rare-0 nodes : {len(rare0)}")
        print(f"[rare_nodes] total nodes  : {len(net_counts)}")

    return {
        'rare1': rare1,
        'rare0': rare0,
        'rare_value': rare_value,
        'net_counts': net_counts,
        'total_nodes': len(net_counts),
        'threshold': threshold,
        'seed_assign': seed_assign,
    }
