"""
compatibility_graph.py
======================
Algorithm 2 of the paper: construction of the *compatibility graph* and
extraction of complete subgraphs (cliques) that serve as trigger-node sets.

Method
------
1. For every rare node `n` (with rare value `r`) run PODEM to obtain a test
   vector that justifies `n = r`.  The vector is reduced to its *care bits*
   over the primary / pseudo inputs.  Nodes for which no vector is found are
   dropped.
2. Two rare nodes are *compatible* when their care-bit vectors can be merged
   without conflict, i.e. every shared input bit agrees.  Compatible pairs
   become edges of an undirected graph whose vertices are the rare nodes.
3. Within this graph, a *complete subgraph* (clique) of size `q` is a set of
   `q` rare nodes that are pairwise compatible and -- because pairwise
   compatibility of care-bit vectors is transitive for a consistent merge --
   are simultaneously satisfiable by a single merged test vector.  Each clique
   is therefore a valid trigger-node set whose activation needs no separate
   validation step.

The merged vector for a clique is returned alongside it; it is the exact
primary/pseudo-input assignment that drives all the clique's rare nodes to
their rare values at once (used later for self-validation).
"""

import random

from . import podem_atpg


# ---------------------------------------------------------------------------
# Test-vector generation per rare node
# ---------------------------------------------------------------------------

def generate_test_vectors(netlist, rare_nodes_info, CC0=None, CC1=None,
                          max_nodes=None, verbose=False,
                          podem_budget=1500, use_podem=True):
    """Return {node: care_bits_dict} for rare nodes.

    For each rare node a care-bit test vector is produced by PODEM; if PODEM
    does not converge within its decision budget, the vector captured during
    rare-node extraction is relaxed to care bits instead (guaranteed to
    succeed).  rare_nodes_info must come from rare_nodes.extract_rare_nodes.
    """
    if CC0 is None or CC1 is None:
        CC0, CC1 = podem_atpg.compute_scoap(netlist)

    rare_value = rare_nodes_info['rare_value']
    seed_assign = rare_nodes_info.get('seed_assign', {})
    candidates = list(rare_value.keys())
    if max_nodes is not None:
        candidates = candidates[:max_nodes]

    tvs = {}
    n_podem = n_relax = n_fail = 0
    for i, node in enumerate(candidates):
        rv = rare_value[node]
        cb = None
        if use_podem:
            vm = podem_atpg.podem(netlist, node, rv, CC0=CC0, CC1=CC1,
                                  max_decisions=podem_budget)
            if vm is not None:
                cb = podem_atpg.care_bits(netlist, vm)
                n_podem += 1
        if cb is None and node in seed_assign:
            cb = podem_atpg.relax_to_care_bits(netlist, seed_assign[node],
                                               node, rv)
            if cb is not None:
                n_relax += 1
        if cb is None:
            n_fail += 1
            continue
        tvs[node] = cb
        if verbose and (i + 1) % 25 == 0:
            print(f"  [tvgen] {i+1}/{len(candidates)} | podem={n_podem} "
                  f"relax={n_relax} fail={n_fail}")
    if verbose:
        print(f"  [tvgen] done: {len(tvs)} vectors "
              f"(podem={n_podem}, relax={n_relax}, fail={n_fail})")
    return tvs


# ---------------------------------------------------------------------------
# Compatibility test
# ---------------------------------------------------------------------------

def compatible(tv1, tv2):
    """Two care-bit dicts are compatible if all shared bits agree."""
    # iterate over the smaller dict for speed
    if len(tv2) < len(tv1):
        tv1, tv2 = tv2, tv1
    for k, v in tv1.items():
        ov = tv2.get(k)
        if ov is not None and ov != v:
            return False
    return True


def merge(tvs):
    """Merge a list of compatible care-bit dicts into one assignment."""
    out = {}
    for tv in tvs:
        out.update(tv)
    return out


# ---------------------------------------------------------------------------
# Graph construction (conflict-set representation -- avoids O(R^2) adjacency)
# ---------------------------------------------------------------------------

class CompatGraph:
    """Compatibility graph stored implicitly via per-(input,value) node groups.

    Two rare nodes are compatible iff no shared care bit conflicts.  Rather than
    materialising the (potentially huge, dense) adjacency, we keep
        by_bit[(input, value)] = set(nodes that require input==value)
    The set of nodes *incompatible* with a node u is then the union, over u's
    care bits (x, v), of by_bit[(x, 1-v)].  This makes clique growth a cheap
    set-difference and scales to thousands of rare nodes.
    """

    def __init__(self, test_vectors):
        self.test_vectors = test_vectors
        self.nodes = list(test_vectors.keys())
        self.by_bit = {}
        for node, cb in test_vectors.items():
            for x, v in cb.items():
                self.by_bit.setdefault((x, v), set()).add(node)
        self._edges = None  # computed lazily / optionally

    def number_of_nodes(self):
        return len(self.nodes)

    def conflicts_of(self, u):
        """Set of nodes incompatible with u (excluding u itself)."""
        s = set()
        bb = self.by_bit
        for x, v in self.test_vectors[u].items():
            opp = bb.get((x, 1 - v))
            if opp:
                s |= opp
        s.discard(u)
        return s

    def neighbors(self, u):
        return set(self.nodes) - self.conflicts_of(u) - {u}

    def compute_edges(self, max_nodes_for_exact=1500):
        """Exact edge count via bitsets (only for modest graphs; else None)."""
        if self._edges is not None:
            return self._edges
        n = len(self.nodes)
        if n > max_nodes_for_exact:
            return None
        # build (mask, val) bitsets over the union of care-bit inputs
        idx = {}
        for cb in self.test_vectors.values():
            for x in cb:
                if x not in idx:
                    idx[x] = len(idx)
        masks, vals = [], []
        for node in self.nodes:
            m = v = 0
            for x, val in self.test_vectors[node].items():
                b = 1 << idx[x]
                m |= b
                if val:
                    v |= b
            masks.append(m)
            vals.append(v)
        edges = 0
        for i in range(n):
            mi, vi = masks[i], vals[i]
            for j in range(i + 1, n):
                if (mi & masks[j] & (vi ^ vals[j])) == 0:
                    edges += 1
        self._edges = edges
        return edges

    def number_of_edges(self):
        return self.compute_edges()


def build_compatibility_graph(test_vectors, verbose=False):
    G = CompatGraph(test_vectors)
    if verbose:
        e = G.compute_edges()
        if e is not None:
            print(f"  [graph] {G.number_of_nodes()} nodes, {e} compatible edges")
        else:
            print(f"  [graph] {G.number_of_nodes()} nodes "
                  f"(edge count skipped for speed)")
    return G


# ---------------------------------------------------------------------------
# Clique / complete-subgraph extraction  (greedy randomised sampler)
# ---------------------------------------------------------------------------

def find_cliques(G, q, N, test_vectors, rare_value, seed=None, verbose=False,
                 max_attempts=None):
    """Find up to N trigger sets of exactly `q` pairwise-compatible rare nodes.

    Instead of enumerating every maximal clique (exponential on dense graphs),
    this grows q-cliques greedily from random seeds: start at a random node,
    keep a candidate set of still-compatible nodes, and repeatedly add a random
    candidate while pruning newly-incompatible ones.  Every produced set is a
    genuine clique (pairwise compatible) because each addition removes all
    nodes conflicting with it.

    Returns a list of dicts: {'nodes', 'rare_value', 'merged_tv'}.
    """
    rng = random.Random(seed)
    nodes = G.nodes
    if not nodes:
        return []

    # Order seeds by degree-ish heuristic: nodes with FEW care bits tend to be
    # compatible with many others, so they seed large cliques.  Cheap proxy:
    care_size = {n: len(test_vectors[n]) for n in nodes}
    seed_order = sorted(nodes, key=lambda n: care_size[n])

    triggers = []
    seen = set()
    if max_attempts is None:
        max_attempts = max(2000, 60 * N)
    attempts = 0
    seed_i = 0
    all_nodes = set(nodes)

    def record(clique):
        key = frozenset(clique)
        if key in seen:
            return False
        merged = {}
        for nd in clique:
            for k, v in test_vectors[nd].items():
                if merged.get(k, v) != v:
                    return False  # defensive; should not happen for a clique
                merged[k] = v
        seen.add(key)
        triggers.append({
            'nodes': list(clique),
            'rare_value': {nd: rare_value[nd] for nd in clique},
            'merged_tv': merged,
        })
        return True

    while len(triggers) < N and attempts < max_attempts:
        attempts += 1
        # rotate through low-care-bit seeds, then random
        if seed_i < len(seed_order):
            start = seed_order[seed_i]
            seed_i += 1
        else:
            start = rng.choice(nodes)

        clique = [start]
        candidates = all_nodes - G.conflicts_of(start) - {start}
        while len(clique) < q and candidates:
            u = rng.choice(tuple(candidates)) if len(candidates) > 1 \
                else next(iter(candidates))
            clique.append(u)
            candidates -= G.conflicts_of(u)
            candidates.discard(u)
        if len(clique) == q:
            record(clique)

    if verbose:
        print(f"  [cliques] produced {len(triggers)} unique trigger set(s) of "
              f"size {q} in {attempts} attempt(s) (requested {N})")
        if not triggers:
            # report the largest clique we could grow to guide the user
            best = 0
            for start in seed_order[:50]:
                clique = [start]
                cand = all_nodes - G.conflicts_of(start) - {start}
                while cand:
                    u = next(iter(cand))
                    clique.append(u)
                    cand -= G.conflicts_of(u)
                    cand.discard(u)
                best = max(best, len(clique))
            print(f"  [cliques] no set of size {q} found; "
                  f"largest clique reachable ~= {best}. Try a smaller q.")
    return triggers


# ---------------------------------------------------------------------------
# One-call convenience wrapper (Algorithm 2 end-to-end)
# ---------------------------------------------------------------------------

def generate_trigger_sets(netlist, rare_nodes_info, q, N,
                          max_nodes=None, seed=None, verbose=False,
                          podem_budget=400):
    CC0, CC1 = podem_atpg.compute_scoap(netlist)
    tvs = generate_test_vectors(netlist, rare_nodes_info, CC0, CC1,
                                max_nodes=max_nodes, verbose=verbose,
                                podem_budget=podem_budget)
    G = build_compatibility_graph(tvs, verbose=verbose)
    triggers = find_cliques(G, q, N, tvs, rare_nodes_info['rare_value'],
                            seed=seed, verbose=verbose)
    return triggers, G, tvs
