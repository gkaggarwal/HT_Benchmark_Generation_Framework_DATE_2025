# Compatibility-Graph-Assisted Automatic Hardware Trojan Insertion Framework

A complete, runnable implementation of the DATE 2025 paper
*"Compatibility Graph Assisted Automatic Hardware Trojan Insertion Framework"*
(Kumar, Shaik, Riaz, Prasad, Ahlawat — IIT Jammu).

Given a gate-level netlist and user parameters (number of trigger nodes `q`,
number of HT instances `N`, rareness threshold), the framework produces
HT-infected netlists whose trojans (a) trigger only under a rare condition and
(b) evade random-pattern and MERO detection — reproducing the paper's headline
result of ~0 % detection coverage.

---

## 1. What this project does (pipeline)

```
 standard-cell netlist (.v)
        │  [0] netlist_converter      class-library .v  ->  simplified .v
        ▼
 simplified netlist
        │  [1] rare_nodes             Algorithm 1: functional simulation -> rare nodes
        ▼
 rare nodes (+ rare values, seed vectors)
        │  [2] podem_atpg             PODEM + SCOAP test vector per rare node
        │      compatibility_graph    Algorithm 2: care-bit compatibility graph
        │                             -> maximal cliques -> q-node trigger sets
        ▼
 trigger sets (+ merged trigger vector)
        │  [3] trojan_insertion       Algorithm 3: gate-biased trigger tree
        │                             + XOR payload  ->  HT-infected netlist
        ▼
 HT-infected netlists
        │  [4] trojan_validation      activation check + random-pattern stealth
        │      mero_detection         MERO-style detection (optional)
        ▼
 summary.json + per-instance results
```

---

## 2. Quick start

Requires Python 3.9+ only (standard library; no third-party packages).

```bash
# Combinational ISCAS-85 benchmark (c2670): 8 trigger nodes, 8 trojans
python main.py netlists/c2670.v -o output/c2670 -q 8 -n 8 \
       --vectors 1000 --detect 3000 --mero

# Sequential ISCAS-89 scan benchmark (s1423, 74 flip-flops)
python main.py netlists/s1423scan.v -o output/s1423 -q 8 -n 5 \
       --vectors 1000 --detect 3000

# Reproduce a small demo for every benchmark
bash run_demo.sh
```

### Choosing the trojan type

Every run asks which **trojan type** to build (or pass `--trojan-type` to skip
the prompt):

* **combinational** – the payload flips the cycle the rare condition holds
  (`payload = source XOR trigger`). Fires immediately, every time the trigger
  is satisfied.
* **sequential** (FSM / counter "time bomb") – the rare condition drives a
  `k`-bit saturating counter; the payload only fires after the rare condition
  has occurred `2**k − 1` times (`payload = source XOR counter_saturated`).
  This is the canonical FSM/counter trojan and is even stealthier, since a
  single rare event is not enough to detonate. The counter is built from added
  scan flip-flops, so the infected netlist becomes (further) sequential.

```bash
# interactive: you'll be prompted to pick 1) combinational or 2) sequential
python main.py netlists/c2670.v -o output/c2670 -q 8 -n 8

# non-interactive, sequential counter that fires after 2**4 - 1 = 15 occurrences
python main.py netlists/c2670.v -o output/c2670_seq -q 8 -n 8 \
       --trojan-type sequential --counter-width 4
```

Sequential trojans are validated by **multi-cycle simulation**: the framework
arms the counter with the merged rare vector, clocks it, and confirms the
payload detonates after exactly `2**k − 1` occurrences (and that an un-armed
random run never detonates). Works for both combinational base circuits
(ISCAS-85) and sequential ones (ISCAS-89, where the trojan flip-flops coexist
with the circuit's own).

### Visualizing a trojan's trigger subgraph

`visualize_trojan.py` takes an HT-infected netlist, asks for the trojan
**payload** net, traces back through the trojan logic to its **trigger (rare)
nodes**, and renders that sub-circuit:

```bash
python visualize_trojan.py output/c2670/c2670_TI/c2670_T0.v          # prompts for payload
python visualize_trojan.py output/c2670/c2670_TI/c2670_T0.v -p n301 -o my_trojan
```

It prints a summary (trojan type, payload, activation signal, the list of
trigger nodes, gate/flip-flop counts) and writes a single image
`<out>.<fmt>`. Choose the format with `-f/--format png|jpg` (default `png`):

```bash
python visualize_trojan.py infected.v -p n301 -o my_trojan -f jpg
```

**The pipeline does this automatically**: every inserted trojan
`<name>_T<i>.v` is accompanied by its own `<name>_T<i>.png` (or `.jpg` via
`--viz-format jpg`) showing that instance's trigger subgraph — since each
instance uses a distinct payload, every image is different. Pass
`--no-visualize` to turn this off.

Trigger nodes are green, the payload red, the original payload source orange,
trigger-logic gates blue, and counter/FSM flip-flops (sequential trojans) gold.
Rendering uses Graphviz `dot` if installed (fed via stdin, so no intermediate
`.dot` file is left behind), otherwise falls back to networkx + matplotlib with
a built-in layered layout (no pygraphviz needed). A `.png`/`.jpg` is produced as
long as **either** Graphviz **or** matplotlib is available.

Output for each run:
* `output/<name>/<name>_conv.v`            – converted (simplified) netlist
* `output/<name>/<name>_TI/<name>_T*.v`    – HT-infected netlists
* `output/<name>/<name>_TI/<name>_T*.png`  – **trigger-subgraph image per trojan**
  (use `--viz-format jpg` for JPEG, or `--no-visualize` to skip)
* `output/<name>/summary.json`             – metrics for every stage/instance
  (each instance records its `payload` and `image` path)

### Example console output (c2670, q=8)

```
=== [1] Algorithm 1: rare-node extraction (N=1000, theta=20%) ===
[rare_nodes] rare-1 nodes : 23
[rare_nodes] rare-0 nodes : 102
=== [2] Algorithm 2: compatibility graph + cliques (q=8) ===
  [graph] 125 nodes, 4655 compatible edges
  [cliques] 434 maximal cliques of size >= 8; max size 33
=== [3] Algorithm 3: insert 8 HT instance(s) ===
   validation: 8/8 triggers fire, 8/8 corrupt the payload under the rare vector
=== [4] Stealth check: 3000 random patterns ===
   avg trigger coverage   : 0.0
   avg detection coverage : 0.0
=== [4b] MERO detection (N=2) ===
  [mero] 1217 vectors; 125/125 rare nodes excited >= 2 times
  [mero] detection: fired 0, corrupted 0 over 1217 vectors
```

---

## 3. Command-line options (`main.py`)

| Flag | Meaning | Default |
|------|---------|---------|
| `netlist` | input gate-level Verilog | – |
| `-o, --outdir` | output directory | `output/run` |
| `-q, --trigger-nodes` | rare trigger nodes per trojan (clique size) | 8 |
| `-n, --instances` | number of HT instances to generate | 10 |
| `--trojan-type` | `combinational` or `sequential` (FSM/counter). If omitted, you are prompted | prompt |
| `--counter-width` | sequential counter width `k`: fires after `2**k - 1` rare occurrences | 4 |
| `--theta` | rareness threshold as fraction of N (paper: 0.20) | 0.20 |
| `--vectors` | random vectors for rare-node extraction | 10000 |
| `--payload` | payload net (default: a safe primary output) | auto |
| `--detect` | random patterns for stealth check (0 = skip) | 10000 |
| `--mero` | also run MERO-style detection | off |
| `--mero-n` | MERO excitation count N | 2 |
| `--podem-budget` | PODEM decision budget before relaxation fallback | 400 |
| `--max-rare` | cap rare nodes fed to the graph (speed) | none |
| `--seed` | RNG seed | 1 |

> For results closest to the paper use `--vectors 10000`. Smaller values run
> much faster and still demonstrate the method; the demo script uses 1000.

---

## 4. Module map (`ht_framework/`)

| File | Role | Paper section |
|------|------|---------------|
| `netlist_converter.py`   | class/Synopsys cell library → simplified netlist | III-A |
| `netlist_simulation.py`  | 3-valued gate-level simulator (FF pseudo-inputs) | III-A |
| `rare_nodes.py`          | rare-node extraction | **Algorithm 1** |
| `podem_atpg.py`          | PODEM + SCOAP, care-bit relaxation | III-C |
| `compatibility_graph.py` | care-bit compatibility graph + cliques | **Algorithm 2** |
| `trojan_insertion.py`    | gate-biased trigger logic + payload insertion | **Algorithm 3** |
| `trojan_validation.py`   | activation check + random-pattern detection | IV-B |
| `mero_detection.py`      | MERO-style detection (evaluation) | II / IV-B |
| `pipeline.py`, `main.py` | orchestration + CLI | – |

---

## 5. How the key algorithms are implemented

**Algorithm 1 — rare nodes.** `N` random vectors are simulated; a node whose
1-count (0-count) is below `theta*N` is a rare-1 (rare-0) node. The first
random vector that drives each rare node to its rare value is cached as a
"seed" for fast care-bit relaxation.

**Algorithm 2 — compatibility graph.** For every rare node a care-bit test
vector justifying its rare value is produced by **PODEM** (SCOAP-guided
backtrace). Hard-to-justify nodes fall back to **test relaxation**: the cached
seed vector is greedily relaxed (X-restoration, restricted to the node's
fan-in cone) to its care bits — guaranteed to succeed because the seed is
known-good. Two rare nodes are *compatible* when their care bits agree on
every shared input. Rather than materialising the dense O(R²) adjacency, the
graph is stored as per-`(input,value)` node groups, so the set of nodes
incompatible with a node is a cheap union of "opposite-value" groups.
Trigger sets are then grown by a **greedy randomised q-clique sampler**: start
at a random node, keep a candidate set of still-compatible nodes, and add
random candidates (pruning conflicts) until size `q`. Every produced set is a
genuine clique, and each yields a single **merged vector** that excites all `q`
rare nodes at once. This replaces full maximal-clique enumeration (which is
exponential on dense graphs) and makes clique generation near-instant even on
the largest benchmarks.

**Algorithm 3 — trigger logic + payload.** Using the paper's output-bias rule,
rare-value-1 nodes feed an AND/NAND tree and rare-value-0 nodes feed an
OR/NOR tree; each gate is biased so its activating output is its *rare* value.
The trees are combined and normalised so that

```
trigger == 1   <=>   every trigger node is simultaneously at its rare value
```

The payload is **always an internal wire** `P` — primary inputs/outputs and the
module port list are never modified. `P`'s driver is rerouted so
`P = P_orig XOR fire`: harmless when `fire = 0`, an observable corruption (it
propagates through `P`'s fanout to the primary outputs) when `fire = 1`. Here
`fire` is the trigger (combinational) or the counter-saturation signal
(sequential).

`P` is chosen to avoid a live combinational feedback loop into the trigger.
Candidate internal wires are tried feedback-free-first (those outside the
trigger fan-in cone, preferring ones observable at an output); for circuits
where the trigger cone covers every wire (e.g. large `q` on c3540, whose rare
nodes sit right at the outputs), in-cone wires are tried too and the framework
keeps the first whose trigger still fires under the merged rare vector —
feedback is usually logically masked under the rare activation condition.
Sequential trojans cannot form a combinational loop (the counter flip-flops
break it), so any internal wire is used directly.

Across the `N` instances of a run, each infected netlist is given a
**different** payload wire (the pipeline tracks used wires and a per-instance
seed shuffles the candidate order), so the benchmark contains diverse payload
sites rather than the same one repeated. `summary.json` reports
`unique_payload_wires`. If a run requests more instances than there are usable
wires, it falls back to reuse only after every distinct wire is taken.

---

## 6. Note on the cell library (important)

The framework accepts **two** standard-cell libraries and auto-detects which
one a netlist uses (override with `--library {auto,class,nangate}`):

### (a) class / Synopsys library
The supplied ISCAS-85 `c*.v` and ISCAS-89 `s*scan.v` benchmarks use a
*class/Synopsys* library (`nnd2s1`, `nor2s3`, `i1s3`, `sdffs1`, `hi1s1`,
`nb1s1`, …). Cell mapping:

| cells | function | note |
|-------|----------|------|
| `i1*`, `ib1*`, `hi1*` | inverter (NOT) | `hi1` proven to invert: c2670/c6288 contain no other single-input inverting cell and no tied-input NAND/NOR, yet are real circuits |
| `nb1*`, `b1*` | buffer (BUF) | |
| `nnd2/3/4*` | NAND | |
| `nor2..6*` | NOR | |
| `and2..9*` | AND | |
| `or2..5*` | OR | |
| `xor2*` / `xnr2*` | XOR / XNOR | |
| `sdff*` | scan D flip-flop | `Q,QBAR,DIN,SDIN` |

### (b) Nangate Open Cell Library (Cadence Genus / FreePDK45)
Netlists synthesised by Cadence Genus with the **NangateOpenCell** library
(`AND2_X1`, `NAND2_X1`, `AOI21_X1`, `OAI22_X1`, `MUX2_X1`, `DFFR_X1`,
`SDFF_X1`, …) are supported by `nangate_converter.py`, which **decomposes**
each complex cell into the primitives the framework already simulates:

| Nangate cell | decomposition |
|--------------|---------------|
| INV / BUF / CLKBUF / TBUF | not / buf |
| AND/OR/NAND/NOR 2-4, XOR2, XNOR2 | direct primitive |
| `AOI*` (e.g. AOI21, AOI22, AOI221) | per-letter AND groups → NOR |
| `OAI*` (e.g. OAI21, OAI22, OAI221) | per-letter OR groups → NAND |
| MUX2 (`Z = S ? B : A`) | (A·!S)+(B·S) |
| FA / HA (full/half adder) | XOR/AND/OR sum + carry |
| DFF, DFFR, DFFS, DFFRS, SDFF\* | sdff; reset/set modelled by gating D (`d &= RN`, `d |= !SN`) |
| LOGIC0 / LOGIC1 / TIEHI / TIELO | constant `1'b0` / `1'b1` |
| FILLCELL / ANTENNA / DECAP / CLKGATE | ignored (no logic) |

AOI/OAI cells are decomposed by **grouping input pins by their letter**
(`A1,A2` → one group, `B1,B2` → another), so the grouping is correct
regardless of pin-order conventions. The decomposition was verified
exhaustively against the cells' Boolean functions. After conversion the
rare-node, compatibility-graph and trojan-insertion stages run unchanged.

```bash
# a Genus/Nangate netlist - library is auto-detected
python main.py my_design_nangate.v -o output/mydesign -q 8 -n 20
# or force it
python main.py my_design_nangate.v -o output/mydesign -q 8 -n 20 --library nangate
```

`buf`/`not` polarity and reset/scan modelling affect fidelity to the *golden*
circuit only; the framework is internally self-consistent because rare values,
trigger logic and validation are all computed on the converted netlist.

---

## 7. Relationship to the originally supplied code

Reused (cleaned and packaged): the gate-level simulator, rare-node extraction,
and the final PODEM+SCOAP ATPG from the supplied notebooks.

Added because they were missing or incompatible:
* **`netlist_converter.py`** — the supplied converter only handled the Nangate
  library; these benchmarks needed a different one.
* **`compatibility_graph.py`** — Algorithm 2 (the paper's core contribution)
  was not present in the supplied code.
* **`trojan_insertion.py`** — the supplied inserter used *random* trigger
  signals; this implements the rare-node / gate-bias method of Algorithm 3.
* Bounded PODEM (decision budget) + relaxation fallback so test-vector
  generation always terminates; fan-in-cone-aware payload selection to avoid
  trigger feedback; validation and MERO evaluation; pipeline + CLI.

---

## 8. Limitations / knobs

* Runtime is now dominated by per-rare-node test-vector generation (PODEM +
  cone-restricted relaxation); graph construction and clique generation are
  near-instant even for thousands of rare nodes. Use `--vectors`, `--max-rare`,
  and `--podem-budget` to trade speed vs. fidelity.
* Some circuits have very many rare nodes (e.g. c3540 yields ~485 at 20 %),
  which makes stage 2 slow. Cap the graph with `--max-rare 150` (or similar)
  for a fast run; the cliques found are still large. Example:

  ```bash
  python main.py netlists/c3540.v -o output/c3540 -q 8 -n 20 \
         --vectors 1000 --max-rare 150 --detect 2000
  ```
* `q` must be ≤ the largest clique found; if no trigger set of size `q` exists,
  lower `q` or raise `--theta` / `--vectors`. The console reports the maximum
  clique size.
* The MERO detector is a compact evaluation variant, not a full ATPG flow.
