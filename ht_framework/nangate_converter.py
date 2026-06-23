"""
nangate_converter.py
====================
Converts a gate-level Verilog netlist synthesised by Cadence Genus (or any
tool) with the **Nangate Open Cell Library** (FreePDK45) into the *simplified*
primitive netlist format used by the rest of this framework.

Why a decomposition converter?
------------------------------
The Nangate library contains complex cells -- AND-OR-INVERT (AOI*),
OR-AND-INVERT (OAI*), multiplexers (MUX2), full/half adders (FA/HA), and many
flip-flop variants (DFF/DFFR/DFFS/DFFRS/SDFF...) -- that the simulator, SCOAP
and PODEM do not understand directly.  Rather than teach every downstream
module about each cell, this converter **decomposes** each Nangate cell into
the primitive gates the framework already supports
(and/or/nand/nor/not/buf/xor/xnor and sdff).  After conversion the existing
rare-node, compatibility-graph and trojan-insertion code runs unchanged.

Supported cells (drive-strength suffix `_X<n>` is ignored):
  * INV, BUF, CLKBUF, TBUF, TINV
  * AND2-4, OR2-4, NAND2-4, NOR2-4, XOR2, XNOR2
  * AOI*  / OAI*   (arbitrary, grouped by pin letter: A1A2 -> one AND/OR group)
  * MUX2
  * FA (full adder), HA (half adder)
  * DFF, DFFR, DFFS, DFFRS, SDFF, SDFFR, SDFFS, SDFFRS, DLH, DLL (latches)
  * LOGIC0 / LOGIC1 / TIEHI / TIELO (constant tie cells)
  * FILLCELL / ANTENNA / DECAP / CLKGATE  (ignored -- no logic)

Reset/set are modelled functionally by gating the flip-flop data
(active-low RN: d &= RN ; active-low SN: d |= !SN), and scan inputs are treated
as functional D (scan-enable = 0), matching how the framework analyses the
functional behaviour of scan circuits.
"""

import re
from collections import defaultdict

OUT_PINS = {'Z', 'ZN', 'Q', 'QN', 'CO', 'S', 'Y', 'OUT', 'CON', 'SO'}

# simple combinational cells -> primitive keyword
_SIMPLE = {
    'INV': 'not', 'INVX': 'not', 'CLKINV': 'not', 'TINV': 'not',
    'BUF': 'buf', 'CLKBUF': 'buf', 'TBUF': 'buf', 'BUFF': 'buf',
    'AND2': 'and', 'AND3': 'and', 'AND4': 'and',
    'OR2': 'or', 'OR3': 'or', 'OR4': 'or',
    'NAND2': 'nand', 'NAND3': 'nand', 'NAND4': 'nand',
    'NOR2': 'nor', 'NOR3': 'nor', 'NOR4': 'nor',
    'XOR2': 'xor', 'XNOR2': 'xnor',
}

_TIE0 = {'LOGIC0', 'TIELO', 'TIEL', 'ZERO'}
_TIE1 = {'LOGIC1', 'TIEHI', 'TIEH', 'ONE'}
_IGNORE = {'FILLCELL', 'FILL', 'ANTENNA', 'DECAP', 'CLKGATE', 'CLKGATETST',
           'TAPCELL', 'WELLTAP', 'ENDCAP'}


def strip_drive(cell):
    """Remove the Nangate drive-strength suffix, e.g. AOI21_X2 -> AOI21.

    Nangate / Genus always uses the underscore form `_X<n>`, so we strip only
    that (stripping a bare `X<n>` would corrupt names like MUX2 -> MU).
    """
    return re.sub(r'_X\d+$', '', cell.upper())


def _letter_groups(pins):
    """Group input pin nets by their leading letter (A1,A2 -> group 'A')."""
    groups = defaultdict(list)
    for k in pins:
        if k in OUT_PINS or k in ('CK', 'CLK', 'GCK', 'G', 'GN'):
            continue
        m = re.match(r'([A-Za-z]+?)(\d*)$', k)
        letter = m.group(1) if m else k
        idx = int(m.group(2)) if (m and m.group(2)) else 0
        groups[letter].append((idx, pins[k]))
    # order nets within a group and order groups alphabetically
    ordered = {}
    for letter in sorted(groups):
        nets = [net for _, net in sorted(groups[letter])]
        ordered[letter] = nets
    return ordered


class _Emitter:
    def __init__(self, prefix):
        self.prefix = prefix
        self.gates = []     # (gtype, out, [ins])
        self.flops = []     # (q, qbar, d, sdin)
        self._uid = 0

    def nw(self, tag):
        self._uid += 1
        return f"{self.prefix}_{tag}{self._uid}"

    def g(self, gtype, out, ins):
        self.gates.append((gtype, out, list(ins)))
        return out


# ---------------------------------------------------------------------------
# Per-cell decomposition
# ---------------------------------------------------------------------------

def decompose_cell(base, pins, em):
    """Decompose one Nangate cell into primitives.  Returns the output net name
    (for combinational cells) or None (flip-flops handled internally)."""

    # ----- ignore / fill -------------------------------------------------
    if base in _IGNORE:
        return None

    # ----- constant tie cells -------------------------------------------
    if base in _TIE0:
        out = pins.get('Z') or pins.get('ZN') or pins.get('Y')
        if out:
            em.g('buf', out, ["1'b0"])
        return out
    if base in _TIE1:
        out = pins.get('Z') or pins.get('ZN') or pins.get('Y')
        if out:
            em.g('buf', out, ["1'b1"])
        return out

    # ----- flip-flops / latches -----------------------------------------
    if base.startswith('DFF') or base.startswith('SDFF') or \
       base.startswith('DLH') or base.startswith('DLL') or \
       base.startswith('LH') or base.startswith('SEDFF'):
        return _decompose_flop(base, pins, em)

    out = pins.get('Z') or pins.get('ZN') or pins.get('Y') or pins.get('OUT')

    # ----- simple primitive gates ---------------------------------------
    if base in _SIMPLE:
        kind = _SIMPLE[base]
        if kind in ('not', 'buf'):
            a = pins.get('A') or pins.get('I') or pins.get('A1')
            em.g(kind, out, [a])
        else:
            ins = []
            for nets in _letter_groups(pins).values():
                ins.extend(nets)
            em.g(kind, out, ins)
        return out

    # ----- AOI / OAI -----------------------------------------------------
    if base.startswith('AOI') or base.startswith('OAI'):
        is_aoi = base.startswith('AOI')
        group_outs = []
        for letter, nets in _letter_groups(pins).items():
            if len(nets) == 1:
                group_outs.append(nets[0])
            else:
                t = em.nw('grp')
                em.g('and' if is_aoi else 'or', t, nets)
                group_outs.append(t)
        if len(group_outs) == 1:
            # degenerate -> just an inverter
            em.g('not', out, group_outs)
        else:
            em.g('nor' if is_aoi else 'nand', out, group_outs)
        return out

    # ----- MUX2 : Z = S ? B : A -----------------------------------------
    if base.startswith('MUX2') or base == 'MUX':
        a = pins.get('A') or pins.get('A0') or pins.get('I0') or pins.get('D0')
        b = pins.get('B') or pins.get('A1') or pins.get('I1') or pins.get('D1')
        s = pins.get('S') or pins.get('S0') or pins.get('SEL')
        ns = em.nw('ns'); em.g('not', ns, [s])
        t1 = em.nw('m'); em.g('and', t1, [a, ns])
        t2 = em.nw('m'); em.g('and', t2, [b, s])
        zo = out or pins.get('Z')
        em.g('or', zo, [t1, t2])
        # MUX2 may be inverting (MUXI/Z vs ZN); if output pin is ZN, invert
        if pins.get('ZN') and not pins.get('Z'):
            inner = em.nw('mz'); em.gates[-1] = ('or', inner, [t1, t2])
            em.g('not', pins['ZN'], [inner])
            return pins['ZN']
        return zo

    # ----- full adder : S = A^B^CI, CO = AB + CI(A^B) -------------------
    if base.startswith('FA'):
        a, b, ci = pins.get('A'), pins.get('B'), pins.get('CI')
        s_out = pins.get('S'); co_out = pins.get('CO') or pins.get('CON')
        ab = em.nw('ab'); em.g('xor', ab, [a, b])
        if s_out:
            em.g('xor', s_out, [ab, ci])
        if co_out:
            t1 = em.nw('co'); em.g('and', t1, [a, b])
            t2 = em.nw('co'); em.g('and', t2, [ab, ci])
            em.g('or', co_out, [t1, t2])
        return s_out or co_out

    # ----- half adder : S = A^B, CO = AB --------------------------------
    if base.startswith('HA'):
        a, b = pins.get('A'), pins.get('B')
        s_out = pins.get('S'); co_out = pins.get('CO') or pins.get('CON')
        if s_out:
            em.g('xor', s_out, [a, b])
        if co_out:
            em.g('and', co_out, [a, b])
        return s_out or co_out

    # ----- unknown: best-effort buffer of first input -------------------
    if out:
        ins = []
        for nets in _letter_groups(pins).values():
            ins.extend(nets)
        if ins:
            em.g('buf', out, [ins[0]])
        return out
    return None


def _decompose_flop(base, pins, em):
    """Map any DFF/SDFF/latch variant to a primitive sdff, modelling reset/set
    by gating the data input.  Returns None (flop handled in em.flops)."""
    q = pins.get('Q') or pins.get('QN')
    qn = pins.get('QN')
    d = pins.get('D') or pins.get('DIN') or pins.get('SI')
    if q is None or d is None:
        return None

    d_eff = d
    # active-low set (SN/SETB): d |= !SN
    sn = pins.get('SN') or pins.get('SETB') or pins.get('SB') or pins.get('S')
    if sn is not None and not base.startswith('SDFF'):  # avoid SDFF scan 'S'
        nsn = em.nw('nsn'); em.g('not', nsn, [sn])
        t = em.nw('setd'); em.g('or', t, [d_eff, nsn]); d_eff = t
    # active-low reset (RN/RESETB): d &= RN  (reset has priority)
    rn = pins.get('RN') or pins.get('RESETB') or pins.get('RB') or pins.get('R')
    if rn is not None:
        t = em.nw('rstd'); em.g('and', t, [d_eff, rn]); d_eff = t

    if qn is None:
        qn = em.nw('qn')
    sdin = pins.get('SI') or d
    em.flops.append((q, qn, d_eff, sdin))
    return None


# ---------------------------------------------------------------------------
# Top-level conversion
# ---------------------------------------------------------------------------

_INSTANCE_RE = re.compile(r'^([A-Za-z][\w]*)\s+(\\?\S+)\s*\((.*)\)\s*;?\s*$',
                          re.S)
_PIN_RE = re.compile(r'\.(\w+)\s*\(\s*([^)]*?)\s*\)')


def _clean(net):
    net = net.strip()
    net = re.sub(r'\]\s+\[', '][', net)
    net = re.sub(r'\s+', '', net)
    return net


def _split_statements(text):
    text = re.sub(r'//[^\n]*', ' ', text)
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    return [s.strip() for s in (' '.join(p.split()) for p in text.split(';'))
            if s.strip()]


def _ports(decl):
    decl = decl.strip()
    names = []
    m = re.match(r'\[(\d+):(\d+)\]\s*(.*)', decl)
    if m:
        hi, lo, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        step = -1 if hi >= lo else 1
        for b in [x.strip() for x in rest.split(',') if x.strip()]:
            for i in range(hi, lo + step, step):
                names.append(f"{b}[{i}]")
        return names
    for tok in decl.split(','):
        tok = re.sub(r'\s+\[', '[', tok.strip())
        if tok:
            names.append(tok)
    return names


def is_nangate_netlist(text):
    """Heuristic: Nangate cells carry a drive-strength suffix like _X1/_X2."""
    return bool(re.search(r'\b[A-Z][A-Z0-9]*_X\d+\b', text))


class ConversionResult:
    def __init__(self, inputs, outputs, wires, n_flops, text):
        self.inputs, self.outputs, self.wires = inputs, outputs, wires
        self.n_flops, self.text = n_flops, text

    @property
    def n_inputs(self):
        return len(self.inputs)

    @property
    def n_outputs(self):
        return len(self.outputs)

    def summary(self):
        return (f"inputs={self.n_inputs}, outputs={self.n_outputs}, "
                f"wires={len(self.wires)}, flip_flops={self.n_flops}")


def convert_nangate_netlist(in_path, out_path, module_name=None, verbose=False):
    with open(in_path) as f:
        text = f.read()
    statements = _split_statements(text)

    module = None
    inputs, outputs, wires = [], [], []
    em = _Emitter('cell')
    declared = set()
    unknown = defaultdict(int)

    for st in statements:
        head = st.split(None, 1)[0] if st.split() else ''
        if head == 'module':
            m = re.match(r'module\s+(\w+)\s*\(', st)
            if m:
                module = module_name or m.group(1)
            continue
        if head in ('endmodule',):
            continue
        if head == 'input':
            inputs.extend(_ports(st[len('input'):]))
            continue
        if head == 'output':
            outputs.extend(_ports(st[len('output'):]))
            continue
        if head in ('wire', 'tri'):
            wires.extend(_ports(st[len(head):]))
            continue
        if head == 'assign':
            m = re.match(r'assign\s+(\S+)\s*=\s*(\S+)', st)
            if m:
                dst, src = _clean(m.group(1)), _clean(m.group(2))
                src = {'1\'b0': "1'b0", '1\'b1': "1'b1"}.get(src, src)
                em.g('buf', dst, [src]); declared.add(dst)
            continue

        m = _INSTANCE_RE.match(st)
        if not m:
            continue
        cell, inst, body = m.group(1), m.group(2), m.group(3)
        if cell in ('module', 'input', 'output', 'wire', 'assign'):
            continue
        base = strip_drive(cell)
        pins = {p: _clean(net) for p, net in _PIN_RE.findall(body) if net.strip()}
        if not pins:
            continue
        nflops_before = len(em.flops)
        out = decompose_cell(base, pins, em)
        if out is None and len(em.flops) == nflops_before and base not in _IGNORE \
                and base not in _TIE0 and base not in _TIE1:
            unknown[base] += 1
        if out:
            declared.add(out)

    # collect declared outputs of emitted gates and flops
    for _, o, _ in em.gates:
        declared.add(o)
    for q, qn, d, sdin in em.flops:
        declared.update([q, qn])

    if module is None:
        module = module_name or 'top'

    wire_set = (set(wires) | declared) - set(inputs) - set(outputs)
    wire_set.discard("1'b0"); wire_set.discard("1'b1")
    wire_list = sorted(wire_set)

    lines = [f"module {module}({', '.join(inputs + outputs)});",
             f"input {', '.join(inputs)};",
             f"output {', '.join(outputs)};"]
    if wire_list:
        lines.append(f"wire {', '.join(wire_list)};")
    for gtype, out, ins in em.gates:
        label = {'and': 'AND', 'or': 'OR', 'nand': 'NAND', 'nor': 'NOR',
                 'not': 'INV', 'buf': 'BUF', 'xor': 'XOR',
                 'xnor': 'XNOR'}[gtype]
        lines.append(f"{gtype} {label}_{_safe(out)}({out}, {', '.join(ins)});")
    for i, (q, qn, d, sdin) in enumerate(em.flops):
        lines.append(f"sdff SDFF_{i}({q}, {qn}, {d}, {sdin});")
    lines.append("endmodule")

    text_out = '\n'.join(lines) + '\n'
    with open(out_path, 'w') as f:
        f.write(text_out)

    if verbose and unknown:
        print("  [nangate] unknown cells (buffered best-effort):",
              dict(unknown))

    return ConversionResult(inputs, outputs, wire_list, len(em.flops), text_out)


def _safe(name):
    return re.sub(r'[^\w]', '_', name)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("usage: python nangate_converter.py <in.v> <out.v>")
        sys.exit(1)
    r = convert_nangate_netlist(sys.argv[1], sys.argv[2], verbose=True)
    print("Converted:", r.summary())
