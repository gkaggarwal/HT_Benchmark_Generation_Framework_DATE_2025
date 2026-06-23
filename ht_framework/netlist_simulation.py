"""
netlist_simulation.py
=====================
Gate-level simulator for the *simplified* netlist format produced by
`netlist_converter.convert_netlist`.  This is a cleaned, self-contained
version of the original `netlist_simulation_package` notebook.

A netlist line has the form

    <type> <name>(<out>, <in1>, <in2>, ...);

where the first signal is the gate output.  Flip-flops use two outputs:

    sdff  SDFF_1(Q, QBAR, DIN, SDIN);
    sdffr SDFFR_1(Q, QBAR, RST, DIN, SDIN);

Flip-flop Q/QBAR are treated as *pseudo inputs*: their present-state value
is supplied externally (random state during rare-node sampling), and the
next-state (the DIN value) is reported back so callers can build state
sequences if desired.
"""

import re

# ---------------------------------------------------------------------------
# Boolean gate primitives
# ---------------------------------------------------------------------------

def AND(inp):  return int(all(inp))
def OR(inp):   return int(any(inp))
def NOT(inp):  return int(not inp[0])
def NAND(inp): return int(not all(inp))
def NOR(inp):  return int(not any(inp))
def XOR(inp):  return int(inp[0] != inp[1])
def XNOR(inp): return int(inp[0] == inp[1])
def BUF(inp):  return int(inp[0])

GATE_FUNCTIONS = {
    'AND': AND, 'OR': OR, 'NOT': NOT, 'NAND': NAND, 'NOR': NOR,
    'XOR': XOR, 'XNOR': XNOR, 'BUF': BUF,
}


class ISCAS85Simulator:
    """Event-free, fixed-point combinational simulator with FF pseudo-inputs."""

    def __init__(self, netlist_file, VERBOSE=False):
        self.VERBOSE = VERBOSE
        self.gates = {}          # output_wire -> {'type','inputs'}
        self.wires = {}          # current logic values
        self.inputs = []         # ordered primary input names
        self.outputs = set()     # primary output names
        self.flip_flops = {}     # name -> {'q','q_bar','data','scan'[,'reset']}
        self.parse_netlist(netlist_file)

    def vprint(self, *a):
        if self.VERBOSE:
            print(*a)

    # -- parsing ---------------------------------------------------------
    def parse_netlist(self, netlist_file):
        with open(netlist_file, 'r') as f:
            for line in f:
                line = line.strip()
                if (not line or line.startswith('//') or
                        line.startswith('module') or line.startswith('endmodule')):
                    continue
                if line.startswith('input'):
                    names = [n for n in re.findall(r'[\w]+\[\d+\]|[\w]+', line)
                             if n.lower() != 'input']
                    self.inputs.extend(names)
                elif line.startswith('output'):
                    self.outputs.update(
                        n for n in re.findall(r'[\w]+\[\d+\]|[\w]+', line)
                        if n.lower() != 'output')
                elif line.startswith('wire'):
                    continue
                elif line.startswith('sdffr'):
                    self.parse_flip_flop_reset(line)
                elif line.startswith('sdff'):
                    self.parse_flip_flop(line)
                else:
                    self.parse_gate(line)

    def parse_gate(self, line):
        m = re.match(r"(\w+)\s+([\w_]+)\(([^)]+)\)", line)
        if not m:
            raise ValueError(f"Could not parse line: {line}")
        gate_type, _, input_str = m.groups()
        gate_type = gate_type.upper()
        wires = [x.strip() for x in input_str.split(',')]
        out_wire, in_wires = wires[0], wires[1:]
        self.gates[out_wire] = {'type': gate_type, 'inputs': in_wires}

    def parse_flip_flop(self, line):
        m = re.match(r"sdff\s+([\w_]+)\(([^)]+)\)", line)
        if not m:
            raise ValueError(f"Could not parse line: {line}")
        name, body = m.groups()
        w = [x.strip() for x in body.split(',')]
        self.flip_flops[name] = {'q': w[0], 'q_bar': w[1], 'data': w[2], 'scan': w[3]}

    def parse_flip_flop_reset(self, line):
        m = re.match(r"sdffr\s+([\w_]+)\(([^)]+)\)", line)
        if not m:
            raise ValueError(f"Could not parse line: {line}")
        name, body = m.groups()
        w = [x.strip() for x in body.split(',')]
        self.flip_flops[name] = {'q': w[0], 'q_bar': w[1],
                                 'reset': w[2], 'data': w[3], 'scan': w[4]}

    # -- stimulus --------------------------------------------------------
    def set_inputs_from_binary(self, binary_stream):
        self.wires = {}
        if len(binary_stream) != len(self.inputs):
            raise ValueError(
                f"Binary stream length {len(binary_stream)} != number of inputs "
                f"{len(self.inputs)}")
        for i, v in enumerate(binary_stream):
            self.wires[self.inputs[i]] = int(v)

    def set_flip_flop_state_from_binary(self, state_stream):
        if len(state_stream) != len(self.flip_flops):
            raise ValueError(
                f"State stream length {len(state_stream)} != number of flops "
                f"{len(self.flip_flops)}")
        for i, (_, ff) in enumerate(self.flip_flops.items()):
            s = int(state_stream[i])
            self.wires[ff['q']] = s
            self.wires[ff['q_bar']] = 1 - s

    def set_named_inputs(self, assignment):
        """Set inputs/pseudo-inputs from a {name: value} dict (others random-free).

        Unspecified inputs/flops are left undriven; caller must ensure
        completeness or use the binary setters.
        """
        for k, v in assignment.items():
            self.wires[k] = int(v)

    # -- evaluation ------------------------------------------------------
    def evaluate_gate(self, gate_name):
        if gate_name in self.wires:
            return self.wires[gate_name]
        if gate_name not in self.gates:
            raise ValueError(f"Unknown gate: {gate_name}")
        gate = self.gates[gate_name]
        gtype = gate['type']
        vals = []
        for nm in gate['inputs']:
            if nm == "1'b0":
                vals.append(0)
            elif nm == "1'b1":
                vals.append(1)
            elif nm in self.wires:
                vals.append(self.wires[nm])
            else:
                return None  # not ready
        if gtype in GATE_FUNCTIONS:
            r = GATE_FUNCTIONS[gtype](vals)
            self.wires[gate_name] = r
            return r
        raise ValueError(f"Unknown gate type: {gtype}")

    def simulate(self):
        remaining = set(self.gates.keys())
        progress = True
        while remaining and progress:
            progress = False
            done = set()
            for g in remaining:
                if self.evaluate_gate(g) is not None:
                    done.add(g)
                    progress = True
            remaining -= done

        value_wire = dict(self.wires)

        outputs = {}
        for o in self.outputs:
            if o in self.wires:
                outputs[o] = self.wires[o]
            elif o == "1'b0":
                outputs[o] = 0
            elif o == "1'b1":
                outputs[o] = 1
            # outputs that are dangling are simply skipped

        # next-state of flip-flops (value present on their data line)
        current_state = {}
        for _, ff in self.flip_flops.items():
            d = ff['data']
            q, qb = ff['q'], ff['q_bar']
            if d in self.wires:
                nv = self.wires[d]
            elif d == "1'b0":
                nv = 0
            elif d == "1'b1":
                nv = 1
            else:
                nv = 0
            current_state[q] = nv
            current_state[qb] = 1 - nv
            value_wire[q] = self.wires.get(q, nv)
            value_wire[qb] = self.wires.get(qb, 1 - nv)

        return value_wire, outputs, current_state


def read_binary_streams_from_file(path):
    with open(path, 'r') as f:
        return [ln.strip() for ln in f if ln.strip()]
