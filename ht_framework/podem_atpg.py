"""
podem_atpg.py
=============
PODEM-style test-pattern generation with SCOAP-guided backtrace, cleaned and
consolidated from the final version in the original
`ATPG_with_Netlist_Simulation` notebook.

For the compatibility-graph construction we need, for a given internal node
`n` and a desired (rare) value `r`, a test vector that justifies `n = r`.
The vector is expressed over the primary inputs *and* the flip-flop
pseudo-inputs (Q / QBAR).  Unassigned inputs stay don't-care ('X'); these
don't-care bits are what allow two vectors to be merged.

Public entry points
-------------------
    parse_netlist(path)                 -> netlist dict
    compute_scoap(netlist)              -> (CC0, CC1)
    podem(netlist, wire, val, ...)      -> value_map | None
    care_bits(netlist, value_map)       -> {pi: 0/1}   (only assigned PI/pseudo)
"""

import re
from copy import deepcopy

# ---------------------------------------------------------------------------
# Gate primitives (multi-input where meaningful)
# ---------------------------------------------------------------------------

def AND(i):  return int(all(i))
def OR(i):   return int(any(i))
def NOT(i):  return int(not i[0])
def NAND(i): return int(not all(i))
def NOR(i):  return int(not any(i))
def XOR(i):  return int(i[0] != i[1])
def XNOR(i): return int(i[0] == i[1])
def BUF(i):  return int(i[0])

GATE_FUNCTIONS = {
    'AND': AND, 'OR': OR, 'NOT': NOT, 'NAND': NAND, 'NOR': NOR,
    'XOR': XOR, 'XNOR': XNOR, 'BUF': BUF,
}


# ---------------------------------------------------------------------------
# Netlist parsing  (simplified format)
# ---------------------------------------------------------------------------

def parse_netlist(filename):
    with open(filename) as f:
        text = f.read()

    inputs = re.search(r'input ([^;]+);', text).group(1).split(',')
    inputs = [i.strip() for i in inputs]
    outputs = re.search(r'output ([^;]+);', text).group(1).split(',')
    outputs = [o.strip() for o in outputs]

    wires = set()
    for m in re.finditer(r'(?:input|output|wire)\s+([^;]+);', text):
        wires.update(w.strip() for w in m.group(1).split(','))
    wires.update(["1'b0", "1'b1"])

    gates = []
    _NON_GATE = {'MODULE', 'INPUT', 'OUTPUT', 'WIRE', 'ENDMODULE', 'ASSIGN'}
    for m in re.finditer(r'(\w+)\s+(\w+)\(([^;]+)\);', text):
        gtype, _, pins = m.groups()
        gtype_u = gtype.upper()
        if gtype_u in _NON_GATE:
            continue
        pinlist = [w.strip() for w in pins.split(',')]
        out_count = 2 if gtype_u in ('SDFF', 'SDFFR') else 1
        outs = pinlist[:out_count]
        ins = pinlist[out_count:]
        gates.append({'type': gtype_u, 'outputs': outs,
                      'output': outs[0], 'inputs': ins})

    netlist = {'inputs': inputs, 'outputs': outputs,
               'wires': list(wires), 'gates': gates}

    driver_of = {}
    for g in gates:
        for o in g['outputs']:
            driver_of[o] = g
    netlist['driver_of'] = driver_of
    return _mark_pseudo_inputs(netlist)


def _mark_pseudo_inputs(netlist):
    pseudo, pairs = [], {}
    for g in netlist['gates']:
        if g['type'] in ('SDFF', 'SDFFR'):
            outs = g.get('outputs') or [g['output']]
            if len(outs) >= 2:
                q, qbar = outs[0], outs[1]
                pseudo.extend([q, qbar])
                pairs[q] = qbar
                pairs[qbar] = q
            else:
                pseudo.extend(outs)
    netlist['pseudo_inputs'] = sorted(set(pseudo))
    netlist['pseudo_pairs'] = pairs
    return netlist


# ---------------------------------------------------------------------------
# SCOAP controllability
# ---------------------------------------------------------------------------

def compute_scoap(netlist):
    CC0, CC1 = {}, {}
    for pi in list(netlist['inputs']) + list(netlist.get('pseudo_inputs', [])):
        CC0[pi] = 1
        CC1[pi] = 1
    CC0["1'b0"], CC1["1'b0"] = 1, 1000
    CC0["1'b1"], CC1["1'b1"] = 1000, 1
    for w in netlist['wires']:
        if w not in CC0:
            CC0[w] = 1_000_000
            CC1[w] = 1_000_000

    changed = True
    while changed:
        changed = False
        for gate in netlist['gates']:
            typ = gate['type']
            if typ in ('SDFF', 'SDFFR'):
                continue
            out = gate['output']
            ins = gate['inputs']
            if any(i not in CC0 or i not in CC1 for i in ins):
                continue
            if typ == 'AND':
                n0 = min(CC0[i] for i in ins) + 1
                n1 = sum(CC1[i] for i in ins) + 1
            elif typ == 'NAND':
                n1 = min(CC0[i] for i in ins) + 1
                n0 = sum(CC1[i] for i in ins) + 1
            elif typ == 'OR':
                n0 = sum(CC0[i] for i in ins) + 1
                n1 = min(CC1[i] for i in ins) + 1
            elif typ == 'NOR':
                n1 = min(CC1[i] for i in ins) + 1
                n0 = sum(CC0[i] for i in ins) + 1
            elif typ in ('XOR', 'XNOR'):
                n0 = sum(CC0[i] for i in ins) + 1
                n1 = sum(CC1[i] for i in ins) + 1
            elif typ in ('BUF',):
                n0 = CC0[ins[0]] + 1
                n1 = CC1[ins[0]] + 1
            elif typ == 'NOT':
                n0 = CC1[ins[0]] + 1
                n1 = CC0[ins[0]] + 1
            else:
                n0 = n1 = 1_000_000
            if n0 < CC0[out] or n1 < CC1[out]:
                CC0[out] = min(n0, CC0[out])
                CC1[out] = min(n1, CC1[out])
                changed = True
    return CC0, CC1


# ---------------------------------------------------------------------------
# Combinational simulation over a partial value_map (3-valued)
# ---------------------------------------------------------------------------

def _eval_order(netlist):
    """Return combinational gates in topological (dependency) order, cached.

    Uses Kahn's algorithm over the gate dependency DAG (gate B precedes gate A
    if B drives an input of A).  Flip-flop outputs are sources (pseudo-inputs),
    so sequential gates are excluded.  Enables a single-pass simulate.
    """
    order = netlist.get('_eval_order')
    if order is not None:
        return order

    from collections import deque
    comb = [g for g in netlist['gates'] if g['type'] not in ('SDFF', 'SDFFR')]
    driver_of = netlist['driver_of']

    # predecessors (driving comb gates) and successors for each comb gate
    preds = {id(g): set() for g in comb}
    succs = {id(g): [] for g in comb}
    gate_by_id = {id(g): g for g in comb}
    for g in comb:
        for inp in g['inputs']:
            drv = driver_of.get(inp)
            if drv is not None and drv['type'] not in ('SDFF', 'SDFFR'):
                if id(drv) != id(g) and id(drv) in preds:
                    if id(drv) not in preds[id(g)]:
                        preds[id(g)].add(id(drv))
                        succs[id(drv)].append(id(g))

    indeg = {gid: len(p) for gid, p in preds.items()}
    q = deque(gid for gid, d in indeg.items() if d == 0)
    order = []
    while q:
        gid = q.popleft()
        order.append(gate_by_id[gid])
        for s in succs[gid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                q.append(s)

    # Any gates left (e.g. in a combinational cycle, not expected) are appended
    if len(order) < len(comb):
        done = {id(g) for g in order}
        order.extend(g for g in comb if id(g) not in done)

    netlist['_eval_order'] = order
    return order


def simulate(netlist, value_map):
    signals = dict(value_map)
    signals["1'b0"] = 0
    signals["1'b1"] = 1
    for gate in _eval_order(netlist):
        out = gate['output']
        if signals.get(out, 'X') != 'X':
            continue
        vals = []
        ready = True
        for inp in gate['inputs']:
            v = signals.get(inp, 'X')
            if v == 'X':
                ready = False
                break
            vals.append(v)
        if not ready:
            continue
        func = GATE_FUNCTIONS.get(gate['type'])
        if not func:
            continue
        try:
            signals[out] = func(vals)
        except (TypeError, IndexError):
            continue
    return signals


# ---------------------------------------------------------------------------
# SCOAP-guided backtrace
# ---------------------------------------------------------------------------

def backtrace(netlist, wire, value, value_map, CC0, CC1):
    primary_or_pseudo = set(netlist['inputs']) | set(netlist.get('pseudo_inputs', []))
    if wire in primary_or_pseudo:
        return [(wire, value)]

    g = netlist['driver_of'].get(wire)
    if not g:
        return []
    if g['type'] in ('SDFF', 'SDFFR'):
        return [(wire, value)]

    gate = g['type'].lower()
    free = [w for w in g['inputs'] if value_map.get(w, 'X') == 'X']
    if not free:
        return []

    def cost(w, v):
        return CC0[w] if v == 0 else CC1[w]

    result = []

    if gate in ('and', 'nand', 'or', 'nor'):
        if gate == 'and':
            mode, ctrl, nonc = ('one', 0, 1) if value == 0 else ('all', 0, 1)
        elif gate == 'nand':
            v = 1 - value
            mode, ctrl, nonc = ('one', 0, 1) if v == 0 else ('all', 0, 1)
        elif gate == 'or':
            mode, ctrl, nonc = ('one', 1, 0) if value == 1 else ('all', 1, 0)
        else:  # nor
            v = 1 - value
            mode, ctrl, nonc = ('one', 1, 0) if v == 1 else ('all', 1, 0)

        if mode == 'one':
            w = min(free, key=lambda x: cost(x, ctrl))
            result.extend(backtrace(netlist, w, ctrl, value_map, CC0, CC1))
        else:
            for w in sorted(free, key=lambda x: cost(x, nonc)):
                result.extend(backtrace(netlist, w, nonc, value_map, CC0, CC1))
        return result

    if gate in ('not', 'inv'):
        w = free[0]
        return backtrace(netlist, w, 1 - value, value_map, CC0, CC1)

    if gate in ('buf', 'buffer'):
        w = free[0]
        return backtrace(netlist, w, value, value_map, CC0, CC1)

    if gate in ('xor', 'xnor'):
        ins = g['inputs']
        if len(ins) != 2:
            for w in sorted(free, key=lambda x: min(CC0[x], CC1[x])):
                v = 0 if CC0[w] <= CC1[w] else 1
                result.extend(backtrace(netlist, w, v, value_map, CC0, CC1))
            return result
        a, b = ins
        freed = [w for w in (a, b) if value_map.get(w, 'X') == 'X']

        def best(w):
            return 0 if CC0[w] <= CC1[w] else 1

        if gate == 'xor':
            if len(freed) == 2:
                va = best(a); vb = va ^ value
                for w, v in sorted([(a, va), (b, vb)], key=lambda t: cost(*t)):
                    result.extend(backtrace(netlist, w, v, value_map, CC0, CC1))
            elif len(freed) == 1:
                w = freed[0]; other = b if w == a else a
                known = value_map.get(other)
                vw = value ^ known if known in (0, 1) else best(w)
                result.extend(backtrace(netlist, w, vw, value_map, CC0, CC1))
        else:  # xnor
            if len(freed) == 2:
                va = best(a); vb = va if value == 1 else 1 - va
                for w, v in sorted([(a, va), (b, vb)], key=lambda t: cost(*t)):
                    result.extend(backtrace(netlist, w, v, value_map, CC0, CC1))
            elif len(freed) == 1:
                w = freed[0]; other = b if w == a else a
                known = value_map.get(other)
                vw = (known if value == 1 else 1 - known) if known in (0, 1) else best(w)
                result.extend(backtrace(netlist, w, vw, value_map, CC0, CC1))
        return result

    # default
    if value == 0 and free:
        w = min(free, key=lambda x: cost(x, 0))
        result.extend(backtrace(netlist, w, 0, value_map, CC0, CC1))
    else:
        for w in sorted(free, key=lambda x: cost(x, 1)):
            result.extend(backtrace(netlist, w, 1, value_map, CC0, CC1))
    return result


# ---------------------------------------------------------------------------
# PODEM
# ---------------------------------------------------------------------------

class _BudgetExceeded(Exception):
    """Raised internally when the PODEM decision budget is used up."""


def podem(netlist, wire, val, value_map=None, CC0=None, CC1=None,
          depth=0, max_depth=200, max_decisions=20000):
    """Bounded PODEM.

    Returns a value_map that justifies `wire = val`, or None if no test is
    found within the decision budget.  The budget guarantees termination even
    on hard-to-justify nodes (which are simply dropped downstream).
    """
    if CC0 is None or CC1 is None:
        CC0, CC1 = compute_scoap(netlist)
    budget = [max_decisions]
    try:
        return _podem(netlist, wire, val, value_map, CC0, CC1,
                      depth, max_depth, budget)
    except _BudgetExceeded:
        return None


def _podem(netlist, wire, val, value_map, CC0, CC1, depth, max_depth, budget):
    if depth > max_depth:
        return None
    budget[0] -= 1
    if budget[0] <= 0:
        raise _BudgetExceeded()

    if value_map is None:
        value_map = {k: 'X' for k in netlist['wires']}

    sim_res = simulate(netlist, value_map)
    if sim_res.get(wire, 'X') == val:
        return value_map

    drivables = set(netlist['inputs']) | set(netlist.get('pseudo_inputs', []))
    if all(value_map.get(pi, 'X') != 'X' for pi in drivables):
        return None

    pairs = netlist.get('pseudo_pairs', {})
    candidates = backtrace(netlist, wire, val, value_map, CC0, CC1)
    if not candidates:
        return None

    # Standard PODEM: pick a SINGLE primary-input objective (the cheapest one
    # produced by the SCOAP-guided backtrace) and branch two ways on it.
    pi, tv = candidates[0]
    if True:
        for try_val in (tv, 1 - tv):
            if value_map.get(pi, 'X') != 'X' and value_map[pi] != try_val:
                continue
            vm = dict(value_map)
            vm[pi] = try_val
            mate = pairs.get(pi)
            if mate is not None:
                comp = 1 - try_val
                ex = vm.get(mate, 'X')
                if ex != 'X' and ex != comp:
                    continue
                vm[mate] = comp
            res = _podem(netlist, wire, val, vm, CC0, CC1,
                         depth + 1, max_depth, budget)
            if res:
                return res
    return None


def care_bits(netlist, value_map):
    """Return only the assigned primary/pseudo-input bits of a value_map."""
    drivables = set(netlist['inputs']) | set(netlist.get('pseudo_inputs', []))
    return {pi: value_map[pi] for pi in drivables
            if value_map.get(pi, 'X') != 'X'}


def fanin_cone_inputs(netlist, target):
    """Return the set of primary/pseudo inputs in the fan-in cone of `target`.

    Flip-flop outputs are cone boundaries (pseudo-inputs).  Inputs outside this
    set cannot influence `target`, so they are automatically don't-care.
    """
    cache = netlist.setdefault('_cone_cache', {})
    if target in cache:
        return cache[target]
    drivables = set(netlist['inputs']) | set(netlist.get('pseudo_inputs', []))
    pseudo = set(netlist.get('pseudo_inputs', []))
    driver_of = netlist['driver_of']
    seen = set()
    ins = set()
    stack = [target]
    while stack:
        w = stack.pop()
        if w in seen:
            continue
        seen.add(w)
        if w in drivables:
            ins.add(w)
            if w in pseudo:
                continue  # boundary
        g = driver_of.get(w)
        if not g or g['type'] in ('SDFF', 'SDFFR'):
            continue
        for s in g['inputs']:
            if s not in seen:
                stack.append(s)
    cache[target] = ins
    return ins


def fanin_cone_gates(netlist, target):
    """Eval-ordered list of gates whose output is in target's fan-in cone.

    Lets relaxation simulate only the logic that can affect `target`, instead
    of the whole circuit -- a large speedup on big benchmarks.
    """
    cache = netlist.setdefault('_cone_gates_cache', {})
    if target in cache:
        return cache[target]
    driver_of = netlist['driver_of']
    in_cone = set()
    stack = [target]
    seen = set()
    while stack:
        w = stack.pop()
        if w in seen:
            continue
        seen.add(w)
        g = driver_of.get(w)
        if g is None or g['type'] in ('SDFF', 'SDFFR'):
            continue
        in_cone.add(id(g))
        for s in g['inputs']:
            if s not in seen:
                stack.append(s)
    order = [g for g in _eval_order(netlist) if id(g) in in_cone]
    cache[target] = order
    return order


def simulate_cone(netlist, value_map, cone_gates):
    """Single-pass simulate restricted to a precomputed cone gate list."""
    signals = dict(value_map)
    signals["1'b0"] = 0
    signals["1'b1"] = 1
    for gate in cone_gates:
        out = gate['output']
        if signals.get(out, 'X') != 'X':
            continue
        vals = []
        ready = True
        for inp in gate['inputs']:
            v = signals.get(inp, 'X')
            if v == 'X':
                ready = False
                break
            vals.append(v)
        if not ready:
            continue
        func = GATE_FUNCTIONS.get(gate['type'])
        if not func:
            continue
        try:
            signals[out] = func(vals)
        except (TypeError, IndexError):
            continue
    return signals


def relax_to_care_bits(netlist, full_assign, target, target_val):
    """Minimise a fully-specified satisfying assignment to its care bits.

    `full_assign` is a {primary/pseudo-input: 0/1} dict (e.g. a random vector
    captured during rare-node extraction) that drives `target` to `target_val`.
    Only inputs in the fan-in cone of `target` are considered; each is greedily
    relaxed to don't-care as long as the target keeps its value.  Returns the
    resulting care-bit dict, or None if the seed does not satisfy the objective.

    This is the standard ATPG "test relaxation" operation; it is guaranteed to
    succeed because it starts from a known-good assignment, and restricting to
    the fan-in cone makes it fast and yields compact (highly mergeable) vectors.
    """
    drivables = set(netlist['inputs']) | set(netlist.get('pseudo_inputs', []))
    pairs = netlist.get('pseudo_pairs', {})
    cone = fanin_cone_inputs(netlist, target)
    cone_gates = fanin_cone_gates(netlist, target)
    # only cone inputs can matter; keep just those from the seed
    care = {k: int(v) for k, v in full_assign.items()
            if k in drivables and k in cone}

    base = {k: 'X' for k in netlist['wires']}

    def sat(assign):
        vm = dict(base)
        vm.update(assign)
        for q, qb in pairs.items():
            if q in assign and qb not in assign:
                vm[qb] = 1 - assign[q]
        return simulate_cone(netlist, vm, cone_gates).get(target, 'X') == target_val

    if not sat(care):
        # cone restriction failed (unexpected); fall back to full assignment
        care = {k: int(v) for k, v in full_assign.items() if k in drivables}
        if not sat(care):
            return None

    for pi in list(care.keys()):
        saved = care.pop(pi)
        if not sat(care):
            care[pi] = saved   # essential bit, restore
    return care
