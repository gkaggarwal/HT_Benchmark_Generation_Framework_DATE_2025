"""
trojan_insertion.py
===================
Algorithm 3 of the paper: stealthy HT trigger-logic generation and insertion.

Trigger logic
-------------
The trigger is a multi-level tree built with the paper's *output-bias* method
(Section III-D).  Each gate is chosen so that the value required to activate
the trojan is the gate's *rare* (biased-against) output:

    * a rare node whose rare value is 1 feeds AND / NAND gates
    * a rare node whose rare value is 0 feeds OR  / NOR  gates

For every gate we track its *satisfied value* -- the output it produces when
all of its inputs sit at their rare values.  Gates are combined level by level
(equal-satisfied-value pairs merged with a biased 2-input gate; a lone
mismatched pair is equalised with an inverter) until a single trigger signal
remains, normalised so that its satisfied value is 1.  Consequently:

    trigger == 1   <=>   every trigger (rare) node is at its rare value

which happens only under the rare condition, so the trojan is stealthy.

Payload
-------
A chosen payload net P is split: its original driver now drives `P_orig`, and
a new gate computes  P = P_orig XOR trigger.  Under normal operation
trigger = 0 and P = P_orig (no effect); under the rare trigger condition
trigger = 1 and P is inverted -- an observable malfunction.
"""

import random
import re

from . import podem_atpg


# ---------------------------------------------------------------------------
# Trigger-tree construction
# ---------------------------------------------------------------------------

# satisfied-output value of each 2-input biased gate given equal input sats
#   inputs both sat=1  -> AND:1 , NAND:0
#   inputs both sat=0  -> OR :0 , NOR :1
_MERGE_OPTIONS = {
    1: [('and', 1), ('nand', 0)],
    0: [('or', 0), ('nor', 1)],
}


class _TriggerBuilder:
    def __init__(self, prefix, rng):
        self.prefix = prefix
        self.rng = rng
        self.gates = []      # list of (gate_type, out_name, [in1, in2 or in1])
        self._uid = 0

    def _new(self, tag):
        self._uid += 1
        return f"{self.prefix}_{tag}_{self._uid}"

    def add_gate(self, gtype, ins):
        out = self._new(gtype)
        self.gates.append((gtype, out, list(ins)))
        return out

    def invert(self, sig):
        out = self._new('inv')
        self.gates.append(('not', out, [sig]))
        return out

    def reduce(self, frontier):
        """frontier: list of (signal, satisfied_value).  Returns final signal
        whose satisfied value is 1."""
        # Guard: single node
        if len(frontier) == 1:
            sig, sat = frontier[0]
            if sat == 0:
                return self.invert(sig), 1
            return sig, 1

        level = list(frontier)
        while len(level) > 1:
            ones = [x for x in level if x[1] == 1]
            zeros = [x for x in level if x[1] == 0]
            nxt = []

            for group, sat_in in ((ones, 1), (zeros, 0)):
                i = 0
                while i + 1 < len(group):
                    a, b = group[i][0], group[i + 1][0]
                    gtype, sat_out = self.rng.choice(_MERGE_OPTIONS[sat_in])
                    out = self.add_gate(gtype, [a, b])
                    nxt.append((out, sat_out))
                    i += 2
                if i < len(group):           # leftover single
                    nxt.append(group[i])

            # If we still have a lone 1 and a lone 0 that never paired, equalise
            if len(nxt) == len(level):       # no merge happened -> mixed leftovers
                # invert one zero to a one and force an AND merge
                z_idx = next((k for k, x in enumerate(nxt) if x[1] == 0), None)
                o_idx = next((k for k, x in enumerate(nxt) if x[1] == 1), None)
                if z_idx is not None and o_idx is not None:
                    zsig = self.invert(nxt[z_idx][0])
                    osig = nxt[o_idx][0]
                    out = self.add_gate('and', [zsig, osig])
                    # remove the two consumed, add merged
                    keep = [x for k, x in enumerate(nxt)
                            if k not in (z_idx, o_idx)]
                    keep.append((out, 1))
                    nxt = keep
                else:
                    break
            level = nxt

        sig, sat = level[0]
        if sat == 0:
            return self.invert(sig), 1
        return sig, 1


def build_trigger_logic(trigger_nodes, rare_value, prefix='troj', seed=None):
    """Return (gates, trigger_signal).

    gates is a list of (gtype, out_name, inputs); trigger_signal is 1 exactly
    under the rare condition.
    """
    rng = random.Random(seed)
    builder = _TriggerBuilder(prefix, rng)

    # Leaves: each rare node enters with satisfied value == its rare value.
    r1 = [(n, 1) for n in trigger_nodes if rare_value[n] == 1]
    r0 = [(n, 0) for n in trigger_nodes if rare_value[n] == 0]
    rng.shuffle(r1)
    rng.shuffle(r0)

    roots = []
    if r1:
        s, _ = builder.reduce(r1)   # AND/NAND tree, normalised to sat=1
        roots.append((s, 1))
    if r0:
        s, _ = builder.reduce(r0)   # OR/NOR tree, normalised to sat=1
        roots.append((s, 1))

    trigger_signal, _ = builder.reduce(roots)
    return builder.gates, trigger_signal


# ---------------------------------------------------------------------------
# Netlist insertion
# ---------------------------------------------------------------------------

def build_counter_logic(trigger_signal, prefix, width):
    """Build a `width`-bit saturating up-counter enabled by `trigger_signal`.

    The counter advances by one each clock cycle in which `trigger_signal` (the
    combinational rare condition) is 1, and saturates (holds) once all bits are
    1.  The payload-activating signal `fire` is asserted exactly when the
    counter is saturated -- i.e. only after the rare condition has occurred
    2**width - 1 times.  This realises a sequential (FSM/counter) trojan: a
    single rare event is not enough; the event must recur many times.

    Returns (comb_gates, ff_specs, fire_signal, q_signals) where
      comb_gates : list of (gtype, out, [ins])   -- combinational next-state logic
      ff_specs   : list of (q, qbar, d, sdin)     -- counter flip-flops
      fire_signal: name of the saturation/activation signal
      q_signals  : list of the counter Q nets (state bits)
    """
    gates = []
    ffs = []
    qs = [f"{prefix}_cntq{i}" for i in range(width)]
    qns = [f"{prefix}_cntqn{i}" for i in range(width)]
    ds = [f"{prefix}_cntd{i}" for i in range(width)]

    gmax = f"{prefix}_cntmax"      # all bits == 1  -> counter saturated
    gates.append(('and', gmax, list(qs)))
    nmax = f"{prefix}_cntnmax"
    gates.append(('not', nmax, [gmax]))
    en0 = f"{prefix}_cnten0"       # increment enable: trigger & not saturated
    gates.append(('and', en0, [trigger_signal, nmax]))

    carry = en0
    for i in range(width):
        gates.append(('xor', ds[i], [qs[i], carry]))   # D_i = Q_i XOR carry_i
        if i < width - 1:
            nc = f"{prefix}_cntc{i+1}"
            gates.append(('and', nc, [carry, qs[i]]))   # carry_{i+1}
            carry = nc
        ffs.append((qs[i], qns[i], ds[i], qs[i]))       # scan-in tied to Q

    fire = gmax
    return gates, ffs, fire, qs


def build_fsm_logic(trigger_signal, prefix, num_states):
    """An `num_states`-state FSM realised as the smallest binary counter that
    can represent it (a counter is the canonical sequential-trojan FSM).
    Delegates to build_counter_logic with width = ceil(log2(num_states))."""
    import math
    width = max(1, math.ceil(math.log2(max(2, num_states))))
    return build_counter_logic(trigger_signal, prefix, width)


def _gate_keyword_label(gtype):
    return {'and': 'AND', 'or': 'OR', 'nand': 'NAND', 'nor': 'NOR',
            'not': 'INV', 'xor': 'XOR', 'buf': 'BUF'}[gtype]


class TrojanInserter:
    """Insert one compatibility-graph-derived HT instance into a netlist."""

    def __init__(self, netlist_file, output_file):
        self.netlist_file = netlist_file
        self.output_file = output_file
        self.lines = []
        self.inputs = []
        self.outputs = []
        self._read()

    def _read(self):
        with open(self.netlist_file) as f:
            self.lines = f.readlines()
        for ln in self.lines:
            s = ln.strip()
            if s.startswith('input'):
                self.inputs += [t for t in re.findall(r'[\w]+\[\d+\]|[\w]+', s)
                                if t != 'input']
            elif s.startswith('output'):
                self.outputs += [t for t in re.findall(r'[\w]+\[\d+\]|[\w]+', s)
                                 if t != 'output']

    def _find_driver_line(self, net):
        """Return index of the gate line whose output (first pin) is `net`."""
        pat = re.compile(r'^\s*\w+\s+[\w_]+\(\s*' + re.escape(net) + r'\s*,')
        single = re.compile(r'^\s*\w+\s+[\w_]+\(\s*' + re.escape(net) + r'\s*\)')
        for i, ln in enumerate(self.lines):
            if pat.match(ln) or single.match(ln):
                return i
        return None

    def _build_fanin_map(self):
        """Map each net to the list of nets that drive it (gate inputs)."""
        fanin = {}
        gate_re = re.compile(r'^\s*(\w+)\s+[\w_]+\(([^;]+)\)\s*;')
        for ln in self.lines:
            m = gate_re.match(ln)
            if not m:
                continue
            gtype = m.group(1).lower()
            pins = [p.strip() for p in m.group(2).split(',')]
            if not pins:
                continue
            out_count = 2 if gtype in ('sdff', 'sdffr') else 1
            outs = pins[:out_count]
            ins = pins[out_count:]
            for o in outs:
                fanin[o] = ins
        return fanin

    def _fanin_cone(self, seeds):
        """Transitive fan-in cone (all influencing nets) of a set of nets.

        Flip-flop outputs are treated as cone boundaries (pseudo-inputs), so
        the cone stays within one combinational frame -- consistent with how
        the rare condition is evaluated.
        """
        fanin = getattr(self, '_fanin', None)
        if fanin is None:
            fanin = self._fanin = self._build_fanin_map()
        ff_outs = self._ff_outputs()
        cone = set()
        stack = list(seeds)
        while stack:
            net = stack.pop()
            if net in cone:
                continue
            cone.add(net)
            if net in ff_outs:
                continue  # boundary
            for src in fanin.get(net, []):
                if src not in cone:
                    stack.append(src)
        return cone

    def _ff_outputs(self):
        outs = getattr(self, '_ffouts', None)
        if outs is not None:
            return outs
        outs = set()
        ff_re = re.compile(r'^\s*sdffr?\s+[\w_]+\(([^;]+)\)\s*;')
        for ln in self.lines:
            m = ff_re.match(ln)
            if m:
                pins = [p.strip() for p in m.group(1).split(',')]
                outs.update(pins[:2])
        self._ffouts = outs
        return outs

    def _internal_wires(self):
        """Gate-driven nets that are neither primary inputs nor outputs."""
        cache = getattr(self, '_intw', None)
        if cache is not None:
            return cache
        pis, pos = set(self.inputs), set(self.outputs)
        internal = []
        seen = set()
        gate_re = re.compile(r'^\s*\w+\s+[\w_]+\(\s*([\w\[\]]+)\s*[,)]')
        for ln in self.lines:
            m = gate_re.match(ln)
            if m:
                w = m.group(1)
                if w not in pis and w not in pos and w not in seen:
                    seen.add(w)
                    internal.append(w)
        self._intw = internal
        return internal

    def _observable_wires(self):
        """Nets that fan out (transitively) to at least one primary output."""
        cache = getattr(self, '_obsw', None)
        if cache is not None:
            return cache
        # fan-in cone of all primary outputs = every net that influences an output
        self._obsw = self._fanin_cone(set(self.outputs))
        return self._obsw

    def payload_candidates(self, prefer=None, avoid_cone=None):
        """Ordered list of *internal-wire* payload candidates.

        The payload is ALWAYS an internal wire -- primary inputs and outputs are
        never selected or modified, and no new port is ever added.  Ordering:
          1. internal, observable (reaches an output), outside the trigger cone
          2. internal, outside the trigger cone
          3. internal, observable                (may have feedback -> validated)
          4. any internal wire
        Combinational trojans should use a feedback-free wire (tiers 1-2); if
        none exist the caller validates tier-3/4 candidates and keeps one whose
        trigger still fires (feedback is often logically masked under the rare
        activation condition).
        """
        avoid = avoid_cone or set()
        internal = self._internal_wires()
        obs = self._observable_wires()
        pis, pos = set(self.inputs), set(self.outputs)

        def ok(w):
            return (w in internal) and (w not in pis) and (w not in pos)

        t1, t2, t3, t4 = [], [], [], []
        for w in internal:
            outside = w not in avoid
            observable = w in obs
            if outside and observable:
                t1.append(w)
            elif outside:
                t2.append(w)
            elif observable:
                t3.append(w)
            else:
                t4.append(w)

        ordered = []
        if prefer and ok(prefer):
            ordered.append(prefer)
        for tier in (t1, t2, t3, t4):
            ordered.extend(tier)
        # dedup, preserve order
        seen = set()
        result = []
        for w in ordered:
            if w not in seen and self._find_driver_line(w) is not None:
                seen.add(w)
                result.append(w)
        if not result:
            raise ValueError("No internal wire available for the payload.")
        return result

    def insert(self, trigger_set, payload_wire, seed=None,
               trojan_prefix='troj', trojan_type='combinational',
               counter_width=4):
        rare_value = trigger_set['rare_value']
        nodes = trigger_set['nodes']

        # 1) combinational rare-condition trigger (common to both types)
        gates, trigger_signal = build_trigger_logic(
            nodes, rare_value, prefix=trojan_prefix, seed=seed)

        # 2) sequential trojan: add a counter that fires only after the rare
        #    condition recurs 2**counter_width - 1 times.
        counter_ffs = []
        counter_qs = []
        fire_signal = trigger_signal
        if trojan_type == 'sequential':
            cgates, counter_ffs, fire_signal, counter_qs = build_counter_logic(
                trigger_signal, trojan_prefix, counter_width)
            gates = gates + cgates

        # The payload is ALWAYS an internal wire; primary inputs/outputs and the
        # module port list are never modified.  Its driver is rerouted:
        #     payload = payload_orig XOR fire_signal
        payload = payload_wire
        if payload in set(self.inputs) | set(self.outputs):
            raise ValueError(
                f"Refusing to use port '{payload}' as payload (internal only).")
        di = self._find_driver_line(payload)
        if di is None:
            raise ValueError(f"Payload wire {payload} has no driver.")

        lines = list(self.lines)
        new_wires = [g[1] for g in gates] + [trigger_signal]
        if trojan_type == 'sequential':
            for q, qbar, d, sdin in counter_ffs:
                new_wires += [q, qbar, d]

        payload_orig = f"{payload}_orig"
        lines[di] = re.sub(r'(\(\s*)' + re.escape(payload) + r'(\s*,)',
                           r'\1' + payload_orig + r'\2', lines[di], count=1)
        lines[di] = re.sub(r'(\(\s*)' + re.escape(payload) + r'(\s*\))',
                           r'\1' + payload_orig + r'\2', lines[di], count=1)
        new_wires.append(payload_orig)
        payload_net = payload
        payload_tap = payload_orig

        new_wires = list(dict.fromkeys(new_wires))

        # add wire declaration for trojan signals (ports untouched)
        wire_added = False
        for i, ln in enumerate(lines):
            if ln.strip().startswith('wire'):
                s = ln.rstrip()
                if s.endswith(';'):
                    s = s[:-1]
                s += ', ' + ', '.join(new_wires) + ';\n'
                lines[i] = s
                wire_added = True
                break
        if not wire_added:
            for i, ln in enumerate(lines):
                if ln.strip().startswith('output'):
                    lines.insert(i + 1, 'wire ' + ', '.join(new_wires) + ';\n')
                    break

        # build trojan gate lines (combinational trigger + counter logic)
        troj_lines = []
        for gtype, out, ins in gates:
            label = _gate_keyword_label(gtype)
            troj_lines.append(
                f"{gtype} {label}_{out}({out}, {', '.join(ins)});\n")
        for idx, (q, qbar, d, sdin) in enumerate(counter_ffs):
            troj_lines.append(
                f"sdff SDFF_{trojan_prefix}_cnt{idx}({q}, {qbar}, {d}, {sdin});\n")
        troj_lines.append(
            f"xor XOR_{trojan_prefix}_payload"
            f"({payload_net}, {payload_tap}, {fire_signal});\n")

        for i, ln in enumerate(lines):
            if ln.strip().startswith('endmodule'):
                lines[i:i] = troj_lines
                break
        else:
            lines += troj_lines + ['endmodule\n']

        with open(self.output_file, 'w') as f:
            f.writelines(lines)

        return {
            'output_file': self.output_file,
            'trojan_type': trojan_type,
            'payload': payload_net,
            'payload_mode': 'reroute',
            'payload_orig': payload_orig,
            'payload_tap': payload_tap,
            'payload_source': payload,
            'trigger_signal': trigger_signal,   # combinational rare condition
            'fire_signal': fire_signal,          # what actually flips the payload
            'counter_width': counter_width if trojan_type == 'sequential' else 0,
            'counter_qs': counter_qs,
            'num_trigger_nodes': len(nodes),
            'num_trojan_gates': len(gates) + 1,
            'num_trojan_ffs': len(counter_ffs),
            'merged_tv': trigger_set.get('merged_tv', {}),
            'trigger_nodes': nodes,
            'rare_value': rare_value,
        }


def _fires_under_merged(infected_file, info):
    """Quick single-cycle check: does the trigger assert (and the payload wire
    flip) under the merged rare vector?  Used to reject combinational payload
    wires whose feedback is NOT logically masked."""
    from . import netlist_simulation
    sim = netlist_simulation.ISCAS85Simulator(infected_file)
    merged = info.get('merged_tv', {})
    in_idx = {n: i for i, n in enumerate(sim.inputs)}
    ff_idx = {}
    is_qbar = {}
    for i, (_, ff) in enumerate(sim.flip_flops.items()):
        ff_idx[ff['q']] = i
        ff_idx[ff['q_bar']] = i
        is_qbar[ff['q_bar']] = True
    ivals = [0] * len(sim.inputs)
    svals = [0] * len(sim.flip_flops)
    for name, val in merged.items():
        if name in in_idx:
            ivals[in_idx[name]] = int(val)
        elif name in ff_idx:
            svals[ff_idx[name]] = (1 - int(val)) if is_qbar.get(name) else int(val)
    sim.set_inputs_from_binary(''.join(map(str, ivals)))
    sim.set_flip_flop_state_from_binary(''.join(map(str, svals)))
    vw, out, _ = sim.simulate()
    # combinational: trigger == fire; sequential: trigger should still assert
    return vw.get(info['trigger_signal']) == 1


def insert_trojan(netlist_file, output_file, trigger_set,
                  payload_signal=None, seed=None, trojan_prefix='troj',
                  trojan_type='combinational', counter_width=4,
                  exclude_payloads=None, payload_seed=None):
    """Insert one HT instance, choosing an INTERNAL-WIRE payload.

    Primary inputs/outputs and the module port list are never modified.  For
    combinational trojans the payload must not create a live feedback loop into
    the trigger; candidates are tried (feedback-free wires first) and the first
    whose trigger still fires under the merged rare vector is kept.  Sequential
    trojans cannot form a combinational loop (the counter flip-flops break it),
    so the first usable internal candidate is used.

    `exclude_payloads` is a set of wires already used by other instances; the
    chosen payload avoids them so every infected netlist gets a *different*
    payload wire (best-effort: it falls back to reuse only if every usable wire
    is taken).  `payload_seed` shuffles the candidate order so the choice also
    varies even without exclusions.
    """
    import random as _random
    ins = TrojanInserter(netlist_file, output_file)
    nodes = trigger_set['nodes']
    avoid = set() if trojan_type == 'sequential' else ins._fanin_cone(nodes)
    candidates = ins.payload_candidates(prefer=payload_signal, avoid_cone=avoid)

    excluded = set(exclude_payloads or ())
    # shuffle within feedback tiers (preserve tier1<tier2<... priority) so the
    # payload varies per instance while keeping feedback-free wires first
    if payload_seed is not None:
        rng = _random.Random(payload_seed)
        obs = ins._observable_wires()
        internal = set(ins._internal_wires())
        def tier_key(w):
            outside = w not in avoid
            observable = w in obs
            return (0 if (outside and observable) else
                    1 if outside else 2 if observable else 3)
        buckets = {}
        for w in candidates:
            buckets.setdefault(tier_key(w), []).append(w)
        for k in buckets:
            rng.shuffle(buckets[k])
        candidates = [w for k in sorted(buckets) for w in buckets[k]]

    # prefer wires not already used by other instances
    ordered = [w for w in candidates if w not in excluded] + \
              [w for w in candidates if w in excluded]

    if trojan_type == 'sequential':
        wire = ordered[0]
        return ins.insert(trigger_set, wire, seed=seed,
                          trojan_prefix=trojan_prefix, trojan_type=trojan_type,
                          counter_width=counter_width)

    last_info = None
    # try unused candidates first (validated), then fall back to used ones
    for wire in ordered[:80]:
        info = ins.insert(trigger_set, wire, seed=seed,
                          trojan_prefix=trojan_prefix, trojan_type=trojan_type,
                          counter_width=counter_width)
        last_info = info
        if _fires_under_merged(output_file, info):
            return info
    # none validated cleanly (rare) -- return the last attempt
    return last_info
