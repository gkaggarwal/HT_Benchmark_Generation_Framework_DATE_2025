"""
mero_detection.py
=================
A compact implementation of MERO (Multiple Excitation of Rare Occurrence,
Chakraborty et al., CHES 2009) used here purely as a *detection* method to
evaluate the stealth of generated HT benchmarks (paper Table II).

MERO idea
---------
Generate a test set that excites every rare node to its rare value at least
`Nmero` times.  Starting from random vectors, each vector is greedily bit-
mutated: a flip is kept if it increases the number of still-under-excited rare
nodes that reach their rare value.  The resulting compact set is then applied
to the HT-infected netlist; a trojan is *detected* if its trigger fires (or its
payload is corrupted) for some vector.

For the proposed compatibility-graph trojans the trigger needs many rare nodes
at their rare values *simultaneously*, which MERO's per-node excitation does
not arrange -- so the expected coverage is ~0, matching the paper.
"""

import random
from . import netlist_simulation


def _activated_rare(value_wire, rare_value):
    return {n for n, rv in rare_value.items() if value_wire.get(n) == rv}


def generate_mero_vectors(conv_file, rare_value, Nmero=2, max_vectors=2000,
                          seed=7, verbose=False):
    """Return a list of (ivec, svec) MERO test vectors."""
    rng = random.Random(seed)
    sim = netlist_simulation.ISCAS85Simulator(conv_file)
    nin, nff = len(sim.inputs), len(sim.flip_flops)

    counts = {n: 0 for n in rare_value}
    vectors = []

    def simulate(iv, sv):
        sim.set_inputs_from_binary(iv)
        sim.set_flip_flop_state_from_binary(sv)
        vw, _, _ = sim.simulate()
        return vw

    while len(vectors) < max_vectors:
        # stop when every rare node excited Nmero times
        if all(c >= Nmero for c in counts.values()):
            break
        iv = [rng.randint(0, 1) for _ in range(nin)]
        sv = [rng.randint(0, 1) for _ in range(nff)]
        ivs = ''.join(map(str, iv)); svs = ''.join(map(str, sv))
        vw = simulate(ivs, svs)
        base = _activated_rare(vw, rare_value)
        # count newly-needed activations
        gain = sum(1 for n in base if counts[n] < Nmero)

        # MERO mutation: try single-bit flips that improve the gain
        improved = True
        tries = 0
        while improved and tries < nin:
            improved = False
            tries += 1
            bit = rng.randrange(nin)
            iv[bit] ^= 1
            ivs2 = ''.join(map(str, iv))
            vw2 = simulate(ivs2, svs)
            act2 = _activated_rare(vw2, rare_value)
            gain2 = sum(1 for n in act2 if counts[n] < Nmero)
            if gain2 > gain:
                gain = gain2; base = act2; ivs = ivs2; improved = True
            else:
                iv[bit] ^= 1  # revert

        for n in base:
            counts[n] += 1
        vectors.append((ivs, svs))

    if verbose:
        done = sum(1 for c in counts.values() if c >= Nmero)
        print(f"  [mero] {len(vectors)} vectors; "
              f"{done}/{len(counts)} rare nodes excited >= {Nmero} times")
    return vectors


def mero_detection(conv_file, infected_file, info, rare_value,
                   Nmero=2, max_vectors=2000, seed=7, verbose=False):
    """Apply a MERO test set to the infected netlist and measure coverage."""
    vectors = generate_mero_vectors(conv_file, rare_value, Nmero,
                                    max_vectors, seed, verbose)
    gsim = netlist_simulation.ISCAS85Simulator(conv_file)
    isim = netlist_simulation.ISCAS85Simulator(infected_file)
    trig_sig = info['trigger_signal']
    payload = info['payload']
    source = info.get('payload_source', payload)

    fired = corrupted = 0
    for ivs, svs in vectors:
        isim.set_inputs_from_binary(ivs)
        isim.set_flip_flop_state_from_binary(svs)
        ivw, iout, _ = isim.simulate()
        if ivw.get(trig_sig) == 1:
            fired += 1
        gsim.set_inputs_from_binary(ivs)
        gsim.set_flip_flop_state_from_binary(svs)
        gvw, gout, _ = gsim.simulate()
        gp = gvw.get(source, gout.get(source))
        ip = ivw.get(payload, iout.get(payload))
        if gp is not None and ip is not None and gp != ip:
            corrupted += 1

    n = max(1, len(vectors))
    res = {
        'mero_vectors': len(vectors),
        'trigger_activations': fired,
        'payload_corruptions': corrupted,
        'trigger_coverage': fired / n,
        'detection_coverage': corrupted / n,
    }
    if verbose:
        print(f"  [mero] detection: fired {fired}, corrupted {corrupted} "
              f"over {len(vectors)} vectors")
    return res
