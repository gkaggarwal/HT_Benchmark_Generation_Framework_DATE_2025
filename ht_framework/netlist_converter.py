"""
netlist_converter.py
=====================
Converts a gate-level Verilog netlist written with the *class / Synopsys*
standard-cell library (the library used by the provided ISCAS-85 `c*.v`
and ISCAS-89 `s*scan.v` benchmarks) into the *simplified* netlist format
consumed by the rest of this framework (the format the original
`netlist_simulation_package` / PODEM code expects):

    input  a, b, c;
    output y, z;
    wire   n1, n2, ...;
    nand   NAND2_1(out, in1, in2);
    nor    NOR2_1(out, in1, in2);
    not    INV_1(out, in);
    buf    BUF_1(out, in);
    sdff   SDFF_1(Q, QBAR, DIN, SDIN);
    endmodule

Each gate line is `type name(output, input1, input2, ...);` where the FIRST
signal is the output.  Flip-flops carry two outputs (Q, QBAR) followed by
their data and scan inputs, matching `ISCAS85Simulator.parse_flip_flop`.

Why a new converter?
--------------------
The shipped `Convert_Netlist_v2` notebook only handles the *Nangate*
library (cells such as `AND2_X1`, `SDFFR_X2`).  The benchmark netlists that
accompany this project use a completely different cell naming scheme, so a
dedicated converter is required.

Cell-library mapping (justification in the project README):
  * inverters (NOT) : i1*, ib1*, hi1*
  * buffers   (BUF) : nb1*, b1*
  * nand            : nnd2*, nnd3*, nnd4*
  * nor             : nor2* ... nor6*
  * and             : and2* ... and9*
  * or              : or2*  ... or5*
  * xor             : xor2*
  * xnor            : xnr2*
  * scan DFF        : sdff*  ->  sdff (Q, QBAR, DIN, SDIN)

`hi1` is proven to be an inverter: c2670 / c6288 contain no other
single-input inverting cell and no tied-input NAND/NOR, yet they are
real combinational circuits, so `hi1` must invert.
"""

import re
from collections import defaultdict

# ---------------------------------------------------------------------------
# Cell-type classification
# ---------------------------------------------------------------------------

def classify_cell(cell):
    """Return the simplified gate keyword for a class-library cell name.

    The drive-strength suffix (s1/s2/s3/s5/s9 ...) is ignored.
    Returns one of: 'nand','nor','and','or','not','buf','xor','xnor','sdff'
    or None if unknown.
    """
    c = cell.lower()

    # Sequential ---------------------------------------------------------
    if c.startswith('sdff') or c.startswith('dff') or c.startswith('sff'):
        return 'sdff'

    # Inverters / buffers (single input) ---------------------------------
    #   i1, ib1, hi1  -> inverter ;  nb1, b1 -> buffer
    if re.match(r'^(i|ib|hi)\d', c):
        return 'not'
    if re.match(r'^(nb|b)\d', c):
        return 'buf'

    # Multi-input combinational ------------------------------------------
    if c.startswith('nnd') or c.startswith('nand'):
        return 'nand'
    if c.startswith('nor'):
        return 'nor'
    if c.startswith('xnr') or c.startswith('xnor'):
        return 'xnor'
    if c.startswith('xor'):
        return 'xor'
    if c.startswith('and'):
        return 'and'
    if c.startswith('or'):
        return 'or'
    return None


# ---------------------------------------------------------------------------
# Low-level parsing helpers
# ---------------------------------------------------------------------------

def _read_text(path):
    with open(path, 'r') as f:
        return f.read()


def _split_statements(text):
    """Split a Verilog body into `;`-terminated statements (newlines folded)."""
    # Remove comments first
    text = re.sub(r'//[^\n]*', ' ', text)
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    statements = []
    for raw in text.split(';'):
        s = ' '.join(raw.split()).strip()
        if s:
            statements.append(s)
    return statements


def _extract_port_names(decl_body):
    """Extract signal names from an input/output/wire declaration body.

    Handles bus ranges such as `[7:0] foo` by expanding to foo[7]..foo[0].
    """
    decl_body = decl_body.strip()
    names = []
    # Detect a range prefix like [7:0]
    m = re.match(r'\[(\d+):(\d+)\]\s*(.*)', decl_body)
    if m:
        hi, lo, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        step = -1 if hi >= lo else 1
        bases = [b.strip() for b in rest.split(',') if b.strip()]
        for b in bases:
            for i in range(hi, lo + step, step):
                names.append(f"{b}[{i}]")
        return names
    # Plain comma-separated list (possibly with individual bus indices)
    for tok in decl_body.split(','):
        tok = tok.strip()
        if tok:
            # normalise `foo [3]` -> `foo[3]`
            tok = re.sub(r'\]\s+\[', '][', tok)
            tok = re.sub(r'\s+\[', '[', tok)
            names.append(tok)
    return names


# Match a single cell instance:  celltype instname ( .Pin(net), .Pin(net), ... )
_INSTANCE_RE = re.compile(
    r'^([A-Za-z][\w]*)\s+(\\?\S+)\s*\((.*)\)$', re.S)

# Match  .PinName(net)  ; net may contain bus indices / backslash names
_PIN_RE = re.compile(r'\.(\w+)\s*\(\s*([^)]*?)\s*\)')


def _clean_net(net):
    net = net.strip()
    net = re.sub(r'\]\s+\[', '][', net)
    net = re.sub(r'\s+', '', net)
    # constants
    if net in ("1'b0", "1'b1"):
        return net
    return net


# ---------------------------------------------------------------------------
# Public conversion routine
# ---------------------------------------------------------------------------

class ConversionResult:
    def __init__(self, inputs, outputs, wires, n_flops, text):
        self.inputs = inputs
        self.outputs = outputs
        self.wires = wires
        self.n_flops = n_flops
        self.text = text

    @property
    def n_inputs(self):
        return len(self.inputs)

    @property
    def n_outputs(self):
        return len(self.outputs)

    def summary(self):
        return (f"inputs={self.n_inputs}, outputs={self.n_outputs}, "
                f"wires={len(self.wires)}, flip_flops={self.n_flops}")


def convert_netlist(in_path, out_path, module_name=None, verbose=False,
                    library='auto'):
    """Convert a gate-level Verilog netlist to the simplified primitive format.

    `library` selects the source cell library:
      * 'auto'    -- detect Nangate (cells with a `_X<n>` drive suffix) vs the
                     class/Synopsys library and dispatch automatically.
      * 'class'   -- the Synopsys/class library (nnd2s1, nor2s3, sdffs1, ...).
      * 'nangate' -- the Nangate Open Cell Library (AND2_X1, AOI21_X1,
                     SDFFR_X1, ...), synthesised e.g. by Cadence Genus.

    Returns a ConversionResult and writes the simplified netlist to `out_path`.
    """
    text = _read_text(in_path)

    if library == 'auto':
        from . import nangate_converter
        library = 'nangate' if nangate_converter.is_nangate_netlist(text) \
            else 'class'
        if verbose:
            print(f"  [convert] detected '{library}' cell library")

    if library == 'nangate':
        from . import nangate_converter
        return nangate_converter.convert_nangate_netlist(
            in_path, out_path, module_name=module_name, verbose=verbose)

    return _convert_class_netlist(in_path, out_path,
                                  module_name=module_name, verbose=verbose)


def _convert_class_netlist(in_path, out_path, module_name=None, verbose=False):
    """Convert a class-library Verilog netlist to the simplified format.

    Returns a ConversionResult describing the converted circuit and writes
    the simplified netlist to `out_path`.
    """
    text = _read_text(in_path)
    statements = _split_statements(text)

    module = None
    inputs, outputs, wires = [], [], []
    gate_lines = []

    # counters per simplified gate keyword (for unique instance names)
    counters = defaultdict(int)
    flop_count = 0

    # collect declared signals so we can build a clean wire list
    declared = set()

    for st in statements:
        head = st.split(None, 1)[0] if st.split() else ''

        if head == 'module':
            m = re.match(r'module\s+(\w+)\s*\((.*)\)', st, re.S)
            if m:
                module = module_name or m.group(1)
            continue
        if head == 'endmodule':
            continue
        if head == 'input':
            inputs.extend(_extract_port_names(st[len('input'):]))
            continue
        if head == 'output':
            outputs.extend(_extract_port_names(st[len('output'):]))
            continue
        if head == 'wire':
            wires.extend(_extract_port_names(st[len('wire'):]))
            continue
        if head == 'assign':
            # assign  dst = src  ->  buf
            m = re.match(r'assign\s+(\S+)\s*=\s*(\S+)', st)
            if m:
                dst, src = _clean_net(m.group(1)), _clean_net(m.group(2))
                counters['buf'] += 1
                gate_lines.append(f"buf BUF_{counters['buf']}({dst}, {src});")
            continue

        # Otherwise: a cell instance ------------------------------------
        m = _INSTANCE_RE.match(st)
        if not m:
            if verbose:
                print("  [skip] unparsed statement:", st[:80])
            continue
        celltype, instname, body = m.group(1), m.group(2), m.group(3)
        kind = classify_cell(celltype)
        if kind is None:
            if verbose:
                print("  [warn] unknown cell type:", celltype, "in", instname[:40])
            continue

        pins = {pin: _clean_net(net) for pin, net in _PIN_RE.findall(body)}

        if kind == 'sdff':
            flop_count += 1
            q = pins.get('Q')
            qn = pins.get('QN')
            din = pins.get('DIN') or pins.get('D')
            sdin = pins.get('SDIN') or pins.get('SI') or din
            if q is None or din is None:
                if verbose:
                    print("  [warn] malformed flop:", instname[:40], pins)
                continue
            if qn is None:
                qn = f"{instname.strip(chr(92))}__QN_{flop_count}"
                qn = re.sub(r'[^\w\[\]]', '_', qn)
                wires.append(qn)
            counters['sdff'] += 1
            gate_lines.append(f"sdff SDFF_{counters['sdff']}({q}, {qn}, {din}, {sdin});")
            declared.update([q, qn])
            continue

        # Combinational cell --------------------------------------------
        out_net = pins.get('Q') or pins.get('Z') or pins.get('ZN') or pins.get('Y')
        in_nets = [pins[p] for p in pins if p not in ('Q', 'Z', 'ZN', 'Y')]
        # Preserve numeric ordering of DIN1, DIN2 ... if present
        din_pins = sorted([p for p in pins if re.match(r'DIN\d*$', p) or re.match(r'[AB]\d*$', p)],
                          key=lambda p: (re.sub(r'\d', '', p), int(re.search(r'\d+', p).group()) if re.search(r'\d+', p) else 0))
        if din_pins:
            in_nets = [pins[p] for p in din_pins]
        if out_net is None or not in_nets:
            if verbose:
                print("  [warn] malformed cell:", instname[:40], pins)
            continue
        counters[kind] += 1
        label = {'nand': 'NAND', 'nor': 'NOR', 'and': 'AND', 'or': 'OR',
                 'not': 'INV', 'buf': 'BUF', 'xor': 'XOR', 'xnor': 'XNOR'}[kind]
        gate_lines.append(
            f"{kind} {label}_{counters[kind]}({out_net}, {', '.join(in_nets)});")
        declared.add(out_net)

    if module is None:
        module = module_name or 'top'

    # Build a complete wire list (every signal that is not a PI/PO) -------
    wire_set = set(wires) | declared
    wire_set -= set(inputs)
    wire_set -= set(outputs)
    wire_set.discard("1'b0")
    wire_set.discard("1'b1")
    wire_list = sorted(wire_set)

    # Emit the simplified netlist ---------------------------------------
    lines = []
    all_ports = inputs + outputs
    lines.append(f"module {module}({', '.join(all_ports)});")
    lines.append(f"input {', '.join(inputs)};")
    lines.append(f"output {', '.join(outputs)};")
    if wire_list:
        lines.append(f"wire {', '.join(wire_list)};")
    lines.extend(gate_lines)
    lines.append("endmodule")

    out_text = '\n'.join(lines) + '\n'
    with open(out_path, 'w') as f:
        f.write(out_text)

    return ConversionResult(inputs, outputs, wire_list, flop_count, out_text)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("usage: python netlist_converter.py <in.v> <out.v>")
        sys.exit(1)
    res = convert_netlist(sys.argv[1], sys.argv[2], verbose=True)
    print("Converted:", res.summary())
