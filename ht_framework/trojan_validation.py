"""
trojan_validation.py
====================
Self-checks for generated HT-infected netlists:

  * verify_trigger        -- confirm the trojan activates under the merged rare
                             vector and produces an observable payload effect.
  * random_detection      -- apply random patterns and measure how often the
                             trigger fires / the payload is corrupted; this is
                             the paper's Trigger-Coverage / Detection-Coverage
                             style stealth check (expected ~0 for the proposed
                             method).

The infected netlist exposes the internal trigger signal as a wire, so the
simulator can read it directly from value_wire.
"""

import random
from . import netlist_simulation


def _build_streams(sim, carebits, rng):
    """Build (input_stream, state_stream) honouring carebits where possible."""
    input_index = {name: i for i, name in enumerate(sim.inputs)}
    ff_q_index = {}
    for i, (_, ff) in enumerate(sim.flip_flops.items()):
        ff_q_index[ff['q']] = i
        ff_q_index[ff['q_bar']] = i  # qbar handled via complement

    ivals = [rng.randint(0, 1) for _ in sim.inputs]
    svals = [rng.randint(0, 1) for _ in sim.flip_flops]

    for name, val in (carebits or {}).items():
        if name in input_index:
            ivals[input_index[name]] = int(val)
        elif name in ff_q_index:
            idx = ff_q_index[name]
            # if it's the Q net set directly; if QBAR, set complement
            is_qbar = any(ff['q_bar'] == name for ff in sim.flip_flops.values())
            svals[idx] = (1 - int(val)) if is_qbar else int(val)
    return ''.join(map(str, ivals)), ''.join(map(str, svals))


def _simulate(sim, ivec, svec):
    sim.set_inputs_from_binary(ivec)
    sim.set_flip_flop_state_from_binary(svec)
    return sim.simulate()


def verify_trigger(golden_file, infected_file, info, seed=0):
    """Drive the merged rare vector and confirm activation + payload effect.

    info is the dict returned by trojan_insertion.insert_trojan and must carry
    'merged_tv', 'trigger_signal', 'payload'.
    Returns a result dict.
    """
    rng = random.Random(seed)
    gsim = netlist_simulation.ISCAS85Simulator(golden_file)
    isim = netlist_simulation.ISCAS85Simulator(infected_file)

    ivec, svec = _build_streams(isim, info['merged_tv'], rng)

    gvw, gout, _ = _simulate(gsim, ivec, svec)
    ivw, iout, _ = _simulate(isim, ivec, svec)

    trig = ivw.get(info['trigger_signal'], None)
    payload = info['payload']
    source = info.get('payload_source', payload)
    golden_payload = gvw.get(source, gout.get(source))
    infected_payload = ivw.get(payload, iout.get(payload))

    # also confirm every trigger node really is at its rare value
    rv = info.get('rare_value', {})
    nodes_ok = all(ivw.get(n) == rv[n] for n in rv)

    return {
        'trigger_fired': trig == 1,
        'trigger_value': trig,
        'all_rare_nodes_satisfied': nodes_ok,
        'golden_payload': golden_payload,
        'infected_payload': infected_payload,
        'payload_corrupted': (golden_payload is not None and
                              infected_payload is not None and
                              golden_payload != infected_payload),
    }


def random_detection(golden_file, infected_file, info, N=10000, seed=123,
                     verbose=False):
    """Apply N random patterns; count trigger activations and payload mismatches.

    Returns counts and coverages (fraction of patterns).  For a stealthy HT
    these should be ~0.
    """
    rng = random.Random(seed)
    gsim = netlist_simulation.ISCAS85Simulator(golden_file)
    isim = netlist_simulation.ISCAS85Simulator(infected_file)

    trig_sig = info['trigger_signal']
    outputs = sorted(gsim.outputs)

    fired = 0
    corrupted = 0
    for _ in range(N):
        ivec = ''.join(rng.choice('01') for _ in isim.inputs)
        svec = ''.join(rng.choice('01') for _ in isim.flip_flops)
        ivw, iout, _ = _simulate(isim, ivec, svec)
        if ivw.get(trig_sig) == 1:
            fired += 1
        gsim.set_inputs_from_binary(ivec)
        gsim.set_flip_flop_state_from_binary(svec)
        gvw, gout, _ = gsim.simulate()
        # detection coverage = any PRIMARY OUTPUT differs (observable effect)
        if any(gvw.get(o, gout.get(o)) != ivw.get(o, iout.get(o))
               for o in outputs):
            corrupted += 1

    res = {
        'patterns': N,
        'trigger_activations': fired,
        'output_corruptions': corrupted,
        'trigger_coverage': fired / N,
        'detection_coverage': corrupted / N,
    }
    if verbose:
        print(f"  [detection] {N} random patterns: "
              f"trigger fired {fired} times, output corrupted {corrupted} times")
    return res


# ===========================================================================
# Sequential (counter/FSM) trojan validation -- multi-cycle simulation
# ===========================================================================

def _ff_q_names(sim):
    """Ordered list of each flip-flop's Q net (matches state-stream order)."""
    return [ff['q'] for ff in sim.flip_flops.values()]


def _state_string(sim, qvals):
    return ''.join(str(qvals[q]) for q in _ff_q_names(sim))


def verify_trigger_sequential(infected_file, info, max_cycles=None, seed=0,
                              verbose=False):
    """Hold the rare condition and confirm the counter eventually FIRES.

    The check is performed on the infected netlist alone: the payload net and
    its source (payload_orig / tapped net) both exist there, so a corruption is
    simply payload != source, which happens exactly when `fire` == 1.  We drive
    the merged rare vector on the controllable inputs (and re-assert any rare
    flip-flop values each cycle) so the combinational trigger stays 1; the
    counter then advances once per cycle and saturates after 2**width - 1
    cycles.
    """
    isim = netlist_simulation.ISCAS85Simulator(infected_file)
    merged = info.get('merged_tv', {})
    width = info.get('counter_width', 0)
    fire_sig = info['fire_signal']
    payload = info['payload']
    source = info.get('payload_tap', payload)  # pre-trojan value wire
    if max_cycles is None:
        max_cycles = (1 << max(1, width)) + 3

    rng = random.Random(seed)
    input_index = {n: i for i, n in enumerate(isim.inputs)}
    q_names = _ff_q_names(isim)
    q_set = set(q_names)

    # held input vector (rare bits fixed, the rest fixed-random)
    ivals = [rng.randint(0, 1) for _ in isim.inputs]
    for n, v in merged.items():
        if n in input_index:
            ivals[input_index[n]] = int(v)
    ivec = ''.join(map(str, ivals))

    # initial flip-flop state: counter (and everything) at 0
    qvals = {q: 0 for q in q_names}

    fired_cycle = None
    for cycle in range(max_cycles):
        # re-assert rare flip-flop values so the trigger stays satisfied
        for n, v in merged.items():
            if n in q_set:
                qvals[n] = int(v)
        svec = _state_string(isim, qvals)
        isim.set_inputs_from_binary(ivec)
        isim.set_flip_flop_state_from_binary(svec)
        vw, out, nxt = isim.simulate()

        if vw.get(fire_sig) == 1:
            fired_cycle = cycle
            payload_v = vw.get(payload, out.get(payload))
            source_v = vw.get(source, out.get(source))
            return {
                'trigger_fired': True,
                'fire_cycle': cycle,
                'cycles_to_fire': cycle,
                'expected_cycles': (1 << width) - 1 if width else 0,
                'payload_corrupted': (payload_v is not None and
                                      source_v is not None and
                                      payload_v != source_v),
            }
        # advance all flip-flops
        for q in q_names:
            qvals[q] = nxt.get(q, 0)

    return {
        'trigger_fired': False,
        'fire_cycle': None,
        'cycles_to_fire': None,
        'expected_cycles': (1 << width) - 1 if width else 0,
        'payload_corrupted': False,
    }


def random_detection_sequential(infected_file, info, N=10000, seed=123,
                                verbose=False):
    """Apply random multi-cycle sequences; count how often the counter FIRES.

    The counter is reset to 0 at the start of each short sequence and fed random
    inputs; `fire` should essentially never assert, since saturation needs the
    rare condition to recur many times in succession.
    """
    isim = netlist_simulation.ISCAS85Simulator(infected_file)
    width = max(1, info.get('counter_width', 1))
    fire_sig = info['fire_signal']
    payload = info['payload']
    source = info.get('payload_tap', payload)  # pre-trojan value wire

    seq_len = (1 << width) + 4
    num_seq = max(1, N // seq_len)
    q_names = _ff_q_names(isim)
    rng = random.Random(seed)

    fired = corrupted = total = 0
    for _ in range(num_seq):
        qvals = {q: 0 for q in q_names}     # reset counter/state
        for _c in range(seq_len):
            ivec = ''.join(rng.choice('01') for _ in isim.inputs)
            svec = _state_string(isim, qvals)
            isim.set_inputs_from_binary(ivec)
            isim.set_flip_flop_state_from_binary(svec)
            vw, out, nxt = isim.simulate()
            total += 1
            if vw.get(fire_sig) == 1:
                fired += 1
                pv = vw.get(payload, out.get(payload))
                sv = vw.get(source, out.get(source))
                if pv is not None and sv is not None and pv != sv:
                    corrupted += 1
            for q in q_names:
                qvals[q] = nxt.get(q, 0)

    res = {
        'patterns': total,
        'sequences': num_seq,
        'sequence_length': seq_len,
        'trigger_activations': fired,
        'payload_corruptions': corrupted,
        'trigger_coverage': fired / max(1, total),
        'detection_coverage': corrupted / max(1, total),
    }
    if verbose:
        print(f"  [detection-seq] {num_seq} sequences x {seq_len} cycles: "
              f"counter fired {fired} times")
    return res


# ---------------------------------------------------------------------------
# Type-aware dispatchers
# ---------------------------------------------------------------------------

def verify(golden_file, infected_file, info, seed=0):
    """Validate activation for either trojan type."""
    if info.get('trojan_type') == 'sequential':
        return verify_trigger_sequential(infected_file, info, seed=seed)
    return verify_trigger(golden_file, infected_file, info, seed=seed)


def detect(golden_file, infected_file, info, N=10000, seed=123, verbose=False):
    """Random-pattern stealth check for either trojan type."""
    if info.get('trojan_type') == 'sequential':
        return random_detection_sequential(infected_file, info, N=N,
                                           seed=seed, verbose=verbose)
    return random_detection(golden_file, infected_file, info, N=N,
                            seed=seed, verbose=verbose)
