"""
pipeline.py
===========
End-to-end driver for the Compatibility-Graph-Assisted Automatic Hardware
Trojan Insertion Framework.

Stages (matching the paper):
  0. Convert the standard-cell netlist to the simplified format.
  1. Algorithm 1 : extract rare nodes by functional simulation.
  2. Algorithm 2 : PODEM test vectors -> compatibility graph -> cliques
                   (trigger-node sets, each with a merged trigger vector).
  3. Algorithm 3 : build gate-biased trigger logic + payload, emit
                   HT-infected netlists.
  4. Validation  : confirm activation under the rare vector and measure
                   stealth against random patterns.
"""

import os
import time
import json

from . import (netlist_converter, rare_nodes, podem_atpg,
               compatibility_graph as cg, trojan_insertion as ti,
               trojan_validation as tv, mero_detection as mero)


def run(netlist_path, outdir,
        q=8, num_instances=10,
        theta_fraction=0.20, num_vectors=10000,
        payload_signal=None,
        trojan_type='combinational', counter_width=4,
        detection_patterns=10000,
        run_mero=False, mero_n=2, mero_max_vectors=2000,
        podem_budget=400,
        max_rare_nodes=None,
        validate_each=True,
        run_detection=True,
        library='auto',
        visualize=True, viz_format='png',
        seed=1, verbose=True):
    """Run the full framework on a single netlist.

    Returns a summary dict (also written to <outdir>/summary.json).
    """
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(netlist_path))[0]
    conv_path = os.path.join(outdir, f"{base}_conv.v")

    log = print if verbose else (lambda *a, **k: None)
    summary = {'netlist': netlist_path, 'q': q, 'num_instances': num_instances,
               'theta_fraction': theta_fraction, 'num_vectors': num_vectors}
    t_start = time.time()

    # -- stage 0: convert ----------------------------------------------
    log(f"\n=== [0] Convert {netlist_path} ===")
    conv = netlist_converter.convert_netlist(netlist_path, conv_path,
                                             library=library, verbose=verbose)
    log("   ", conv.summary())
    summary['convert'] = {'inputs': conv.n_inputs, 'outputs': conv.n_outputs,
                          'flip_flops': conv.n_flops, 'wires': len(conv.wires)}

    # -- stage 1: rare nodes -------------------------------------------
    log(f"\n=== [1] Algorithm 1: rare-node extraction "
        f"(N={num_vectors}, theta={theta_fraction*100:.0f}%) ===")
    t0 = time.time()
    rn = rare_nodes.extract_rare_nodes(conv_path, theta_fraction, num_vectors,
                                       seed=seed, verbose=verbose)
    summary['rare_nodes'] = {'rare1': len(rn['rare1']), 'rare0': len(rn['rare0']),
                             'total_nodes': rn['total_nodes'],
                             'threshold': rn['threshold'],
                             'time_s': round(time.time() - t0, 2)}

    # -- stage 2: compatibility graph + cliques ------------------------
    log(f"\n=== [2] Algorithm 2: compatibility graph + cliques (q={q}) ===")
    t0 = time.time()
    net = podem_atpg.parse_netlist(conv_path)
    triggers, G, tvs = cg.generate_trigger_sets(
        net, rn, q=q, N=num_instances, max_nodes=max_rare_nodes,
        seed=seed, verbose=verbose, podem_budget=podem_budget)
    summary['compatibility_graph'] = {
        'test_vectors': len(tvs),
        'graph_nodes': G.number_of_nodes(),
        'graph_edges': G.number_of_edges(),
        'trigger_sets': len(triggers),
        'time_s': round(time.time() - t0, 2),
    }
    if not triggers:
        log("   !! no trigger sets of the requested size were found; "
            "try a smaller q or larger N/theta.")
        summary['instances'] = []
        _write_summary(outdir, summary)
        return summary

    # -- stage 3 + 4: insert + validate --------------------------------
    log(f"\n=== [3] Algorithm 3: insert {len(triggers)} "
        f"{trojan_type} HT instance(s)"
        + (f" (counter width {counter_width}, fires after "
           f"{(1<<counter_width)-1} occurrences)" if trojan_type == 'sequential'
           else "") + " ===")
    ti_dir = os.path.join(outdir, f"{base}_TI")
    os.makedirs(ti_dir, exist_ok=True)

    instances = []
    used_payloads = set()
    t0 = time.time()
    for i, trig in enumerate(triggers):
        out_v = os.path.join(ti_dir, f"{base}_T{i}.v")
        info = ti.insert_trojan(conv_path, out_v, trig,
                                payload_signal=payload_signal,
                                seed=seed * 1000 + i,
                                trojan_type=trojan_type,
                                counter_width=counter_width,
                                exclude_payloads=used_payloads,
                                payload_seed=seed * 7919 + i)
        used_payloads.add(info['payload'])
        rec = {
            'index': i,
            'file': out_v,
            'trojan_type': info['trojan_type'],
            'trigger_nodes': info['trigger_nodes'],
            'num_trigger_nodes': info['num_trigger_nodes'],
            'num_trojan_gates': info['num_trojan_gates'],
            'num_trojan_ffs': info['num_trojan_ffs'],
            'counter_width': info['counter_width'],
            'payload': info['payload'],
            'trigger_signal': info['trigger_signal'],
            'fire_signal': info['fire_signal'],
        }
        if validate_each:
            v = tv.verify(conv_path, out_v, info, seed=0)
            rec['verify'] = v
        if visualize:
            try:
                from . import trojan_visualize
                img_base = out_v[:-2] if out_v.endswith('.v') else out_v
                files, vsum = trojan_visualize.generate_image(
                    out_v, info['payload'], img_base, fmt=viz_format)
                rec['image'] = vsum.get('image')
            except Exception as e:
                rec['image'] = None
                rec['image_error'] = str(e)
        instances.append((info, rec))

    n_unique_payloads = len(used_payloads)
    if visualize:
        n_imgs = sum(1 for _, r in instances if r.get('image'))
        log(f"   visualizations: {n_imgs}/{len(instances)} trojan image(s) "
            f"written (.{viz_format})")

    insert_time = time.time() - t0
    summary['insertion'] = {
        'instances': len(instances),
        'trojan_type': trojan_type,
        'counter_width': counter_width if trojan_type == 'sequential' else 0,
        'unique_payload_wires': n_unique_payloads,
        'total_time_s': round(insert_time, 3),
        'avg_time_per_instance_s': round(insert_time / max(1, len(instances)), 5),
    }
    log(f"   payload wires: {n_unique_payloads} distinct across "
        f"{len(instances)} instance(s)")

    # validation summary
    if validate_each:
        n_fire = sum(1 for _, r in instances if r['verify']['trigger_fired'])
        n_corrupt = sum(1 for _, r in instances if r['verify']['payload_corrupted'])
        if trojan_type == 'sequential':
            cycles = [r['verify'].get('cycles_to_fire') for _, r in instances
                      if r['verify'].get('cycles_to_fire') is not None]
            avg_cyc = round(sum(cycles) / len(cycles), 1) if cycles else None
            log(f"   validation: {n_fire}/{len(instances)} counters fire "
                f"(avg {avg_cyc} cycles to fire), "
                f"{n_corrupt}/{len(instances)} corrupt the payload")
            summary['validation'] = {'trigger_fired': n_fire,
                                     'payload_corrupted': n_corrupt,
                                     'avg_cycles_to_fire': avg_cyc,
                                     'instances': len(instances)}
        else:
            log(f"   validation: {n_fire}/{len(instances)} triggers fire, "
                f"{n_corrupt}/{len(instances)} corrupt the payload under the rare vector")
            summary['validation'] = {'trigger_fired': n_fire,
                                     'payload_corrupted': n_corrupt,
                                     'instances': len(instances)}

    # -- detection (stealth) on a sample -------------------------------
    if run_detection:
        log(f"\n=== [4] Stealth check: {detection_patterns} random patterns ===")
        sample = instances[:min(5, len(instances))]
        det_results = []
        tot_trig = tot_det = 0
        for info, rec in sample:
            d = tv.detect(conv_path, rec['file'], info,
                          N=detection_patterns, seed=999)
            rec['detection'] = d
            det_results.append(d)
            tot_trig += d['trigger_coverage']
            tot_det += d['detection_coverage']
        n = len(sample)
        summary['detection'] = {
            'sampled_instances': n,
            'avg_trigger_coverage': round(tot_trig / n, 6) if n else None,
            'avg_detection_coverage': round(tot_det / n, 6) if n else None,
            'patterns_per_instance': detection_patterns,
        }
        log(f"   avg trigger coverage   : {summary['detection']['avg_trigger_coverage']}")
        log(f"   avg detection coverage : {summary['detection']['avg_detection_coverage']}")

    # -- MERO detection (optional) -------------------------------------
    if run_mero and triggers and trojan_type == 'combinational':
        log(f"\n=== [4b] MERO detection (N={mero_n}) ===")
        sample = instances[:min(3, len(instances))]
        tot_t = tot_d = 0.0
        for info, rec in sample:
            m = mero.mero_detection(conv_path, rec['file'], info,
                                    rn['rare_value'], Nmero=mero_n,
                                    max_vectors=mero_max_vectors, verbose=verbose)
            rec['mero'] = m
            tot_t += m['trigger_coverage']
            tot_d += m['detection_coverage']
        k = len(sample)
        summary['mero'] = {
            'sampled_instances': k,
            'avg_trigger_coverage': round(tot_t / k, 6) if k else None,
            'avg_detection_coverage': round(tot_d / k, 6) if k else None,
        }
        log(f"   MERO avg trigger coverage   : {summary['mero']['avg_trigger_coverage']}")
        log(f"   MERO avg detection coverage : {summary['mero']['avg_detection_coverage']}")

    summary['instances'] = [r for _, r in instances]
    summary['total_time_s'] = round(time.time() - t_start, 2)
    _write_summary(outdir, summary)
    log(f"\n=== Done in {summary['total_time_s']}s. "
        f"HT-infected netlists in {ti_dir} ===")
    return summary


def _write_summary(outdir, summary):
    with open(os.path.join(outdir, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2)
