#!/usr/bin/env python3
"""
spice_eval.py -- interactive general-purpose evaluation tool for ngspice netlists.

Just run it with no arguments:

    python3 spice_eval.py

It will:
  1. let you pick a .cir netlist from the current folder,
  2. let you pick an evaluation to run,
  3. ask a few follow-up questions specific to that evaluation,
  4. run ngspice and print the result.

Handles a common KiCad export gotcha automatically: components left
unconnected on the schematic (e.g. a reference pot) get node names like
'unconnected-_POT1-Pad1_'. ngspice's operating-point solver can fail on
these (singular matrix), so this tool patches a 1G-ohm resistor to GND
onto each such node before simulating. This does not change circuit
behavior; it only gives the solver a DC path. The original file on disk
is never modified.

Requires ngspice on PATH.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

NETLIST_EXTS = (".cir", ".sp", ".spice", ".net", ".ckt")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def ask(prompt, default=None):
    try:
        s = input(prompt).strip()
    except EOFError:
        return default if default is not None else ""
    return s if s else (default if default is not None else "")


def choose_netlist():
    files = sorted(f for f in os.listdir(".")
                    if f.lower().endswith(NETLIST_EXTS) and os.path.isfile(f))
    if not files:
        typed = ask("No netlist files found here. Enter a path (or blank to quit): ")
        if not typed:
            sys.exit(0)
        return typed

    print("Netlists in this folder:")
    for i, f in enumerate(files, 1):
        print(f"  [{i}] {f}")
    while True:
        sel = ask(f"Pick a file [1-{len(files)}] (default 1): ", default="1")
        if sel.isdigit() and 1 <= int(sel) <= len(files):
            return files[int(sel) - 1]
        if os.path.exists(sel):
            return sel
        print("Invalid choice, try again.")


def parse_spice_value(s):
    """Convert SPICE-style value strings ('500k', '2.2u', '1Meg') to float."""
    s = s.strip()
    m = re.match(r'^([\d.]+)\s*([a-zA-Z]*)$', s)
    if not m:
        raise ValueError(f"Could not parse value: {s}")
    num, suffix = m.group(1), m.group(2).lower()
    mult = {'': 1, 'f': 1e-15, 'p': 1e-12, 'n': 1e-9, 'u': 1e-6,
             'm': 1e-3, 'k': 1e3, 'meg': 1e6, 'g': 1e9}
    factor = mult['meg'] if suffix.startswith('meg') else mult.get(suffix)
    if factor is None:
        raise ValueError(f"Unknown unit suffix: {suffix}")
    return float(num) * factor


def format_ohms(value):
    if value >= 1e6:
        return f"{value/1e6:.3f}Meg"
    if value >= 1e3:
        return f"{value/1e3:.3f}k"
    return f"{value:.1f}"


def patch_floating_nodes(text):
    """Add a 1G-ohm resistor to GND for every 'unconnected-...' node found.
    Fixes ngspice operating-point convergence on truly floating pins
    without altering circuit behavior or touching the original file."""
    nodes = sorted(set(re.findall(r'\bunconnected-\S+\b', text)))
    if not nodes:
        return text, []
    ghost_lines = [f"Rghost_patch{i} {node} GND 1G" for i, node in enumerate(nodes, 1)]
    if ".end" in text:
        idx = text.rstrip().rfind(".end")
        text = text[:idx] + "\n".join(ghost_lines) + "\n" + text[idx:]
    else:
        text = text + "\n" + "\n".join(ghost_lines) + "\n"
    return text, nodes


def replace_source_value(text, ref, value_str):
    """Replace the value field of a '<ref> <node1> <node2> <value> ...' line."""
    pattern = re.compile(rf'^({re.escape(ref)}\s+\S+\s+\S+\s+)(\S+)(.*)$', re.MULTILINE)
    new_text, n = pattern.subn(rf'\g<1>{value_str}\g<3>', text)
    return new_text, n


def zero_sin_amplitude(text, ref):
    """Set a SIN(...) source's amplitude to 0, to simulate 'no input signal'."""
    pattern = re.compile(
        rf'^({re.escape(ref)}\s+\S+\s+\S+\s+DC\s+\S+\s+SIN\(\s*\S+\s+)(\S+)(\s+.*\))',
        re.MULTILINE
    )
    new_text, n = pattern.subn(r'\g<1>0\g<3>', text)
    return new_text, n > 0


def build_tran_netlist(base_text, probes, tstep, tstop, out_path):
    """Insert a .tran + wrdata control block just before the final .end."""
    probe_str = " ".join(probes)
    control = f"""
.tran {tstep} {tstop}
.control
run
set wr_vecnames
set wr_singlescale
wrdata {out_path} {probe_str}
.endc
"""
    if ".end" in base_text:
        idx = base_text.rstrip().rfind(".end")
        return base_text[:idx] + control + "\n.end\n"
    return base_text + control + "\n.end\n"


def run_ngspice(cir_path):
    return subprocess.run(["ngspice", "-b", cir_path], capture_output=True, text=True)


def read_wrdata(path, probes):
    """Read a wrdata output file.

    ngspice's wrdata is inconsistent: for V(...) probes it writes a header
    line ('time v(x) v(y)'), but for i(...) (current) probes it writes NO
    header line at all -- the file starts directly with numeric data. To
    handle both cases reliably, this checks whether the first line looks
    numeric; if so, there is no header and columns are assigned positionally
    using `probes` (in the order they were passed to wrdata). If not, the
    first line is treated as the real header.

    Returns dict: lower-cased column name -> list[float], including 'time'.
    """
    with open(path) as f:
        first_line = f.readline()
    first_tokens = first_line.split()

    def looks_numeric(tok):
        try:
            float(tok)
            return True
        except ValueError:
            return False

    has_header = not (first_tokens and all(looks_numeric(t) for t in first_tokens))

    if has_header:
        header = [h.lower() for h in first_tokens]
        data_start_line = 1
    else:
        header = ["time"] + [p.lower() for p in probes]
        data_start_line = 0

    data = {h: [] for h in header}
    with open(path) as f:
        for i, line in enumerate(f):
            if i < data_start_line:
                continue
            parts = line.split()
            if len(parts) != len(header):
                continue
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                continue
            for h, v in zip(header, vals):
                data[h].append(v)
    return data


def extract_node_names(text):
    """Scan a netlist for plausible node/net names to suggest to the user."""
    nodes = set()
    numeric_re = re.compile(r'^[+-]?[\d.]+([a-zA-Z]{0,3})?$')
    skip_words = {"DC", "AC", "SIN", "PULSE", "PWL", "EXP", "SFFM"}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('*', '.', '+')):
            continue
        toks = line.split()
        if len(toks) < 3:
            continue
        for tok in toks[1:5]:
            if numeric_re.match(tok):
                continue
            if tok.upper() in skip_words:
                continue
            if tok.startswith(('"', "'", "(")):
                continue
            if any(c in tok for c in "=()+*/"):
                continue
            if tok in ("V", "I"):
                continue
            if tok.startswith("__"):
                continue
            if tok in ("POT_MODEL", "kicad_builtin_opamp"):
                continue
            nodes.add(tok)
    return nodes


def extract_labeled_nodes(text):
    """Return only user-assigned net labels (bf_in, vca_out, JACK_IN, ...),
    filtering out KiCad's auto-generated node names (Net-_XXX_,
    unconnected-_XXX_, probe_int_XXX) so the user isn't shown internal
    plumbing they never named themselves."""
    nodes = extract_node_names(text)
    auto_patterns = ("net-_", "unconnected-", "probe_int_", "#pwr")
    labeled = [n for n in nodes if not n.lower().startswith(auto_patterns)]
    return labeled


def zero_all_ac_sources(text):
    """Set AC magnitude to 0 on every independent V/I source that has one,
    so a single injected test current is the only AC excitation in the
    circuit (needed for a clean impedance-looking-into-a-node measurement).
    DC values are left untouched so the bias point is unaffected."""
    def repl(m):
        return m.group(1) + "0"
    new_text = re.sub(r'^([VI]\w+\s+\S+\s+\S+\s+DC\s+\S+\s+AC\s+)\S+',
                       repl, text, flags=re.MULTILINE)
    return new_text


def extract_voltage_sources(text):
    """Scan a netlist for independent voltage source references (V1, V2, ...)."""
    return sorted(set(re.findall(r'^(V\w+)\s', text, re.MULTILINE)))


def suggest_names(candidates, keyword=None, limit=12):
    """Order candidate names, prioritizing ones containing `keyword`."""
    if keyword:
        prioritized = sorted(n for n in candidates if keyword.lower() in n.lower())
        rest = sorted(n for n in candidates if keyword.lower() not in n.lower())
        ordered = prioritized + rest
    else:
        ordered = sorted(candidates)
    return ordered[:limit]


def print_node_suggestions(text, keyword=None, label="Nodes found in this netlist"):
    nodes = extract_node_names(text)
    suggestions = suggest_names(nodes, keyword=keyword)
    if suggestions:
        print(f"{label}: {' '.join(suggestions)}")


def get_source_voltage(text, ref):
    """Look up a DC voltage source's actual value from the netlist, e.g.
    'V3 +4.5V GND DC 4.5' -> 4.5. Returns None if not found/parseable."""
    m = re.search(rf'^{re.escape(ref)}\s+\S+\s+\S+\s+DC\s+([\d.eE+-]+)', text, re.MULTILINE | re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def print_source_suggestions(text, label="Voltage sources found"):
    sources = extract_voltage_sources(text)
    if sources:
        print(f"{label}: {' '.join(sources)}")


def ensure_v_wrap(s):
    """Turn a bare node name ('vca_out') into 'V(vca_out)' if not already
    wrapped. Leaves already-wrapped or expression-like input untouched."""
    s = s.strip()
    if not s:
        return s
    if re.match(r'^[a-zA-Z]\(', s):
        return s
    return f"V({s})"
    with open(cirfile) as f:
        text = f.read()
    text, patched = patch_floating_nodes(text)
    if patched:
        print("Floating nodes found, patched with 1G-ohm ghost resistors to GND:")
        print(f"  {', '.join(patched)}")
        print()
    return text


def find_isolated_islands(text):
    """Detect groups of nodes that connect only to each other and never to
    GND/0 (a 'floating island'), as opposed to a single unconnected pin.
    Works by building a node-adjacency graph from two/three-terminal
    component lines and finding connected components that don't contain
    GND or '0'. This mirrors the real bug found in this project: a
    sub-circuit (e.g. a charge pump) whose power connection was left out
    when the block was extracted standalone."""
    adjacency = {}

    def link(a, b):
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('*', '.', '+')):
            continue
        toks = line.split()
        if len(toks) < 3:
            continue
        ref = toks[0].upper()
        # Only look at simple element lines where the leading tokens are
        # genuinely node names (R/C/L/D/V/I and the first two nodes of
        # transistor/subckt lines); skip control/model lines entirely.
        if ref[0] not in "RCLDVIJQMX":
            continue
        node_slice = toks[1:3] if ref[0] in "RCLDVI" else toks[1:4]
        node_slice = [n for n in node_slice if not re.match(r'^[+-]?[\d.]+[a-zA-Z]*$', n)]
        for i in range(len(node_slice)):
            for j in range(i + 1, len(node_slice)):
                link(node_slice[i], node_slice[j])

    if not adjacency:
        return []

    ground_aliases = {"GND", "0"}
    visited = set()
    islands = []
    for start in adjacency:
        if start in visited:
            continue
        stack = [start]
        component = set()
        while stack:
            n = stack.pop()
            if n in component:
                continue
            component.add(n)
            stack.extend(adjacency.get(n, ()))
        visited |= component
        if not (component & ground_aliases) and len(component) > 1:
            islands.append(sorted(component))
    return islands


def patch_islands(text, islands):
    """Give each floating island a DC path to GND by tying EVERY node in the
    island to GND via a 1G-ohm ghost resistor each. (A single representative
    node was tried first, but proved insufficient when the island contains
    nonlinear devices like diodes -- ngspice would hang instead of crash.
    Patching every node gives the solver a well-conditioned path everywhere.)
    Used for sub-circuits that are legitimately irrelevant to the simulation
    (e.g. an LED charge pump) rather than a real wiring mistake."""
    if not islands:
        return text
    ghost_lines = []
    i = 0
    for isl in islands:
        for node in isl:
            i += 1
            ghost_lines.append(f"Rislandpatch{i} {node} GND 1G")
    if ".end" in text:
        idx = text.rstrip().rfind(".end")
        text = text[:idx] + "\n".join(ghost_lines) + "\n" + text[idx:]
    else:
        text = text + "\n" + "\n".join(ghost_lines) + "\n"
    return text


def load_and_patch(cirfile):
    with open(cirfile) as f:
        text = f.read()
    text, patched = patch_floating_nodes(text)
    if patched:
        print("Floating nodes found, patched with 1G-ohm ghost resistors to GND:")
        print(f"  {', '.join(patched)}")
        print()

    islands = find_isolated_islands(text)
    if islands:
        print("Floating sub-circuit(s) found (nodes with no DC path to GND,")
        print("e.g. an LED charge pump) -- patched with a 1G-ohm ghost resistor")
        print("each so they don't block simulation:")
        for isl in islands:
            shown = isl[:8]
            more = f" (+{len(isl)-8} more)" if len(isl) > 8 else ""
            print(f"  Island: {', '.join(shown)}{more}")
        text = patch_islands(text, islands)
        print()

    return text


# --------------------------------------------------------------------------
# [1] Power consumption
# --------------------------------------------------------------------------

def eval_power(base_text, tmpdir, cirfile):
    print("\n--- Power consumption ---")
    print_source_suggestions(base_text)
    supply_str = ask("Supply voltage source(s) to sum current from, space-separated: ")
    supplies = supply_str.split()
    if not supplies:
        print("No supply sources given, aborting.")
        return

    supply_voltages = {}
    unresolved = []
    for s in supplies:
        v = get_source_voltage(base_text, s)
        if v is None:
            unresolved.append(s)
        else:
            supply_voltages[s] = abs(v)
    if supply_voltages:
        shown = ", ".join(f"{s}={v:g}V" for s, v in supply_voltages.items())
        print(f"Voltage read from netlist for each supply: {shown}")
    if unresolved:
        fallback = float(ask(
            f"Could not read a DC voltage for {', '.join(unresolved)} -- "
            f"enter a value to use for them (default 9): ", default="9"))
        for s in unresolved:
            supply_voltages[s] = fallback

    input_src = ask("Input signal source to silence for the 'quiescent' case (blank to skip): ")
    tstep = ask("Time step (default 10u): ", default="10u")
    tstop = ask("Final time (default 30m): ", default="30m")

    def measure(text, tag):
        probes = [f"i({s})" for s in supplies]
        out_path = os.path.join(tmpdir, f"power_{tag}.txt")
        cir_path = os.path.join(tmpdir, f"power_{tag}.cir")
        netlist = build_tran_netlist(text, probes, tstep, tstop, out_path)
        with open(cir_path, "w") as f:
            f.write(netlist)
        proc = run_ngspice(cir_path)
        if not os.path.exists(out_path):
            print(f"  [{tag}] ngspice run failed.")
            if proc.stderr:
                print("   stderr:", proc.stderr.strip().splitlines()[-1])
            return None
        data = read_wrdata(out_path, probes)
        times = data.get("time", [])
        if not times:
            return None
        tmax = max(times)
        window_start = tmax * 0.5
        idxs = [i for i, t in enumerate(times) if t > window_start]
        total_i = 0.0
        total_p = 0.0
        breakdown = []
        for s, key in zip(supplies, [p.lower() for p in probes]):
            series = data.get(key, [])
            if not series:
                continue
            avg_i = abs(sum(series[i] for i in idxs) / len(idxs))
            total_i += avg_i
            total_p += avg_i * supply_voltages[s]
            breakdown.append((s, avg_i, supply_voltages[s]))
        return total_i, total_p, breakdown

    print("\n=== Playing (input as-is) ===")
    r_playing = measure(base_text, "playing")
    if r_playing:
        for name, i, v in r_playing[2]:
            print(f"  {name}: {i*1000:.3f} mA @ {v:g}V")
        print(f"  Total current: {r_playing[0]*1000:.3f} mA")
        print(f"  Power: {r_playing[1]*1000:.2f} mW")

    r_quiet = None
    if input_src:
        quiet_text, changed = zero_sin_amplitude(base_text, input_src)
        if not changed:
            print(f"\nWarning: could not zero '{input_src}' amplitude; skipping quiescent case.")
        else:
            print("\n=== Quiescent (input amplitude = 0) ===")
            r_quiet = measure(quiet_text, "quiet")
            if r_quiet:
                for name, i, v in r_quiet[2]:
                    print(f"  {name}: {i*1000:.3f} mA @ {v:g}V")
                print(f"  Total current: {r_quiet[0]*1000:.3f} mA")
                print(f"  Power: {r_quiet[1]*1000:.2f} mW")

    if r_playing and r_quiet:
        print("\n=== Summary ===")
        print(f"Quiescent: {r_quiet[1]*1000:.2f} mW ({r_quiet[0]*1000:.3f} mA)")
        print(f"Playing  : {r_playing[1]*1000:.2f} mW ({r_playing[0]*1000:.3f} mA)")
        cap = ask("\nBattery capacity in mAh, to estimate life (blank to skip): ")
        if cap:
            try:
                cap_v = float(cap)
                avg_ma = max(r_playing[0], r_quiet[0]) * 1000
                hours = cap_v / avg_ma if avg_ma > 0 else float("inf")
                print(f"Estimated battery life: ~{hours:.0f} hours (~{hours/24:.1f} days)")
            except ValueError:
                pass


# --------------------------------------------------------------------------
# [2] DC offset check (chosen signal chain)
# --------------------------------------------------------------------------

def eval_offset(base_text, tmpdir, cirfile):
    print("\n--- DC offset check ---")
    print_node_suggestions(base_text, keyword="out")
    signals_str = ask("Signals to check, space-separated (e.g. V(bf_out) V(ota_out)): ")
    signals = [ensure_v_wrap(s) for s in signals_str.split()]
    if not signals:
        print("No signals given, aborting.")
        return
    tstep = ask("Time step (default 10u): ", default="10u")
    tstop = ask("Final time (default 30m): ", default="30m")
    settle = float(ask("Fraction of the run to treat as 'settled' (default 0.5): ", default="0.5"))

    out_path = os.path.join(tmpdir, "offset.txt")
    cir_path = os.path.join(tmpdir, "offset.cir")
    netlist = build_tran_netlist(base_text, signals, tstep, tstop, out_path)
    with open(cir_path, "w") as f:
        f.write(netlist)
    proc = run_ngspice(cir_path)
    if not os.path.exists(out_path):
        print("ngspice run failed.")
        if proc.stderr:
            print("stderr:", proc.stderr.strip().splitlines()[-1])
        return

    data = read_wrdata(out_path, signals)
    times = data.get("time", [])
    if not times:
        print("No data produced.")
        return
    tmax = max(times)
    window_start = tmax * (1 - settle)
    idxs = [i for i, t in enumerate(times) if t > window_start]

    print(f"\n{'signal':<22}{'DC offset (mV)':>16}{'pp amplitude (V)':>18}")
    print("-" * 56)
    prev_offset = None
    for sig in signals:
        key = sig.lower()
        series = data.get(key, [])
        if not series:
            print(f"{sig:<22}  (not found in output)")
            continue
        seg = [series[i] for i in idxs]
        dc_mv = sum(seg) / len(seg) * 1000
        pp = max(seg) - min(seg)
        delta_str = ""
        if prev_offset is not None:
            delta_str = f"  (stage delta: {dc_mv - prev_offset:+.2f} mV)"
        print(f"{sig:<22}{dc_mv:16.3f}{pp:18.4f}{delta_str}")
        prev_offset = dc_mv


# --------------------------------------------------------------------------
# [3] Gain / amplitude ratio (single run)
# --------------------------------------------------------------------------

def eval_gain(base_text, tmpdir, cirfile):
    print("\n--- Gain / amplitude ratio ---")
    print_node_suggestions(base_text)
    in_sig = ensure_v_wrap(ask("Input signal: "))
    out_sig = ensure_v_wrap(ask("Output signal: "))
    if not in_sig or not out_sig:
        print("Both signals are required, aborting.")
        return
    tstep = ask("Time step (default 10u): ", default="10u")
    tstop = ask("Final time (default 30m): ", default="30m")
    w0 = float(ask("Window start, as fraction of run (default 0.3): ", default="0.3"))
    w1 = float(ask("Window end, as fraction of run (default 0.5): ", default="0.5"))

    out_path = os.path.join(tmpdir, "gain.txt")
    cir_path = os.path.join(tmpdir, "gain.cir")
    netlist = build_tran_netlist(base_text, [in_sig, out_sig], tstep, tstop, out_path)
    with open(cir_path, "w") as f:
        f.write(netlist)
    proc = run_ngspice(cir_path)
    if not os.path.exists(out_path):
        print("ngspice run failed.")
        if proc.stderr:
            print("stderr:", proc.stderr.strip().splitlines()[-1])
        return

    data = read_wrdata(out_path, [in_sig, out_sig])
    times = data.get("time", [])
    if not times:
        print("No data produced.")
        return
    tmax = max(times)
    t0, t1 = tmax * w0, tmax * w1
    idxs = [i for i, t in enumerate(times) if t0 <= t <= t1]
    in_series = data.get(in_sig.lower(), [])
    out_series = data.get(out_sig.lower(), [])
    if not in_series or not out_series:
        print("Could not find one of the signals in the output.")
        return
    in_win = [in_series[i] for i in idxs]
    out_win = [out_series[i] for i in idxs]
    in_pp = max(in_win) - min(in_win)
    out_pp = max(out_win) - min(out_win)
    ratio = out_pp / in_pp if in_pp > 0 else float("nan")

    print(f"\n{in_sig} pp : {in_pp:.5f}")
    print(f"{out_sig} pp: {out_pp:.5f}")
    print(f"Ratio (out/in): {ratio:.4f}")
    if ratio > 0:
        import math
        print(f"In dB: {20*math.log10(ratio):.2f} dB")


# --------------------------------------------------------------------------
# [4] Resistor divider sweep
# --------------------------------------------------------------------------

def eval_sweep(base_text, tmpdir, cirfile):
    print("\n--- Resistor divider sweep ---")
    print("Sweeps R1 while keeping R1+R2 = total, and reports the out/in")
    print("amplitude ratio for each split.")
    r1 = ask("R1 reference (e.g. R24): ")
    r2 = ask("R2 reference (e.g. R20): ")
    total_str = ask("R1+R2 total (e.g. 500k): ")
    values_str = ask("R1 values to try, comma-separated (e.g. 10k,50k,100k,200k): ")
    print_node_suggestions(base_text)
    in_sig = ensure_v_wrap(ask("Input signal: "))
    out_sig = ensure_v_wrap(ask("Output signal: "))
    tstep = ask("Time step (default 10u): ", default="10u")
    tstop = ask("Final time (default 100m): ", default="100m")
    w0 = float(ask("Window start, as fraction of run (default 0.3): ", default="0.3"))
    w1 = float(ask("Window end, as fraction of run (default 0.5): ", default="0.5"))

    if not all([r1, r2, total_str, values_str, in_sig, out_sig]):
        print("Missing input, aborting.")
        return

    total = parse_spice_value(total_str)
    values = [v.strip() for v in values_str.split(",")]

    print(f"\n{'R1('+r1+')':>12}{'ratio %':>10}{'in_pp':>12}{'out_pp':>12}{'out/in':>12}")
    print("-" * 58)

    for v_str in values:
        v = parse_spice_value(v_str)
        r2_val = total - v
        if r2_val < 0:
            print(f"{v_str:>12}  skipped (R1 > total)")
            continue
        r2_str = format_ohms(r2_val)

        text, _ = replace_source_value(base_text, r1, v_str)
        text, _ = replace_source_value(text, r2, r2_str)

        out_path = os.path.join(tmpdir, f"sweep_{v_str}.txt")
        cir_path = os.path.join(tmpdir, f"sweep_{v_str}.cir")
        netlist = build_tran_netlist(text, [in_sig, out_sig], tstep, tstop, out_path)
        with open(cir_path, "w") as f:
            f.write(netlist)
        proc = run_ngspice(cir_path)
        if not os.path.exists(out_path):
            print(f"{v_str:>12}  ngspice run failed")
            continue

        data = read_wrdata(out_path, [in_sig, out_sig])
        times = data.get("time", [])
        if not times:
            print(f"{v_str:>12}  no data produced")
            continue
        tmax = max(times)
        t0, t1 = tmax * w0, tmax * w1
        idxs = [i for i, t in enumerate(times) if t0 <= t <= t1]
        in_series = data.get(in_sig.lower(), [])
        out_series = data.get(out_sig.lower(), [])
        if not in_series or not out_series:
            print(f"{v_str:>12}  signal not found in output")
            continue
        in_win = [in_series[i] for i in idxs]
        out_win = [out_series[i] for i in idxs]
        in_pp = max(in_win) - min(in_win)
        out_pp = max(out_win) - min(out_win)
        ratio = out_pp / in_pp if in_pp > 0 else float("nan")
        pct = v / total * 100
        print(f"{v_str:>12}{pct:9.1f}%{in_pp:12.5f}{out_pp:12.5f}{ratio:12.4f}")


# --------------------------------------------------------------------------
# [5] Export a plain .tran to CSV
# --------------------------------------------------------------------------

def eval_export(base_text, tmpdir, cirfile):
    print("\n--- Export .tran to CSV ---")
    print_node_suggestions(base_text)
    signals_str = ask("Signals to export, space-separated: ")
    signals = [ensure_v_wrap(s) for s in signals_str.split()]
    if not signals:
        print("No signals given, aborting.")
        return
    tstep = ask("Time step (default 10u): ", default="10u")
    tstop = ask("Final time (default 100m): ", default="100m")
    default_out = os.path.splitext(os.path.basename(cirfile))[0] + "_tran.csv"
    out_csv = ask(f"Output CSV filename (default {default_out}): ", default=default_out)

    raw_path = out_csv + ".raw"
    cir_path = os.path.join(tmpdir, "export.cir")
    netlist = build_tran_netlist(base_text, signals, tstep, tstop, raw_path)
    with open(cir_path, "w") as f:
        f.write(netlist)
    proc = run_ngspice(cir_path)
    if not os.path.exists(raw_path):
        print("ngspice run failed.")
        if proc.stderr:
            print("stderr:", proc.stderr.strip().splitlines()[-1])
        return

    with open(raw_path) as f:
        lines = f.readlines()
    with open(out_csv, "w") as f:
        f.write(",".join(lines[0].split()) + "\n")
        for line in lines[1:]:
            parts = line.split()
            if parts:
                f.write(",".join(parts) + "\n")
    os.remove(raw_path)
    print(f"Wrote {out_csv}")


def eval_noise(base_text, tmpdir, cirfile):
    print("\n--- Circuit noise analysis (.noise) ---")
    print("Computes the circuit's own noise (thermal noise etc.), integrated")
    print("over a frequency band, using ngspice's native noise analysis.")
    print_node_suggestions(base_text, keyword="out")
    out_sig = ensure_v_wrap(ask("Output node: "))
    print_source_suggestions(base_text, label="AC-capable sources found (must have 'AC 1' set)")
    in_src = ask("Input AC source name: ")
    if not out_sig or not in_src:
        print("Both are required, aborting.")
        return
    fstart = ask("Start frequency in Hz (default 20): ", default="20")
    fstop = ask("Stop frequency in Hz (default 20k): ", default="20k")
    ppd = ask("Points per decade (default 10): ", default="10")

    control = f"""
.control
noise {out_sig} {in_src} dec {ppd} {fstart} {fstop}
print onoise_total
print inoise_total
.endc
"""
    if ".end" in base_text:
        idx = base_text.rstrip().rfind(".end")
        netlist = base_text[:idx] + control + "\n.end\n"
    else:
        netlist = base_text + control + "\n.end\n"

    cir_path = os.path.join(tmpdir, "noise.cir")
    with open(cir_path, "w") as f:
        f.write(netlist)
    proc = run_ngspice(cir_path)
    out = proc.stdout

    m_on = re.search(r"onoise_total\s*=\s*([\d.eE+-]+)", out)
    m_in = re.search(r"inoise_total\s*=\s*([\d.eE+-]+)", out)
    if not m_on or not m_in:
        print("Could not parse noise results. Raw ngspice output:")
        print(out.strip())
        if proc.stderr:
            print("stderr:", proc.stderr.strip())
        return

    onoise = float(m_on.group(1))
    inoise = float(m_in.group(1))

    print(f"\nIntegrated over {fstart} Hz - {fstop} Hz:")
    print(f"  Output-referred noise (onoise_total): {onoise*1e6:.3f} uV RMS")
    print(f"  Input-referred noise  (inoise_total): {inoise*1e6:.3f} uV RMS")

    ref_str = ask("\nSignal amplitude to compare against, in V peak (blank to skip SNR): ")
    if ref_str:
        try:
            ref_v = float(ref_str)
            ref_rms = ref_v / (2 ** 0.5)
            if onoise > 0:
                import math
                snr_db = 20 * math.log10(ref_rms / onoise)
                print(f"  Estimated SNR at output: {snr_db:.1f} dB "
                      f"(signal {ref_v}Vpk vs. {onoise*1e6:.3f}uV RMS noise)")
        except ValueError:
            pass


# --------------------------------------------------------------------------
# [7] unwanted_comp_check
# --------------------------------------------------------------------------

def set_sin_amplitude(text, ref, amp_str):
    """Set a SIN(...) source's amplitude to a given value."""
    pattern = re.compile(
        rf'^({re.escape(ref)}\s+\S+\s+\S+\s+DC\s+\S+\s+SIN\(\s*\S+\s+)(\S+)(\s+.*\))',
        re.MULTILINE
    )
    new_text, n = pattern.subn(rf'\g<1>{amp_str}\g<3>', text)
    return new_text, n > 0


def eval_amp_sweep(base_text, tmpdir, cirfile):
    print("\n--- Amplitude sweep (gain vs input level) ---")
    print("Sweeps a SIN source's amplitude and reports out/in ratio for each level.")
    print("Use this to spot unintended compression: ratio should stay ~constant.")
    print_source_suggestions(base_text)
    src_ref = ask("SIN source reference to sweep (e.g. V9): ")
    values_str = ask("Amplitudes to try, comma-separated (e.g. 0.005,0.01,0.02,0.05,0.1,0.2,0.5,1.0): ")
    print_node_suggestions(base_text)
    in_sig = ensure_v_wrap(ask("Input signal (probe node, e.g. jack_in): "))
    out_sig = ensure_v_wrap(ask("Output signal (probe node, e.g. ts_out): "))
    tstep = ask("Time step (default 5u): ", default="5u")
    tstop = ask("Final time (default 60m): ", default="60m")
    w0 = float(ask("Window start, as fraction of run (default 0.3): ", default="0.3"))
    w1 = float(ask("Window end, as fraction of run (default 0.8): ", default="0.8"))

    if not all([src_ref, values_str, in_sig, out_sig]):
        print("Missing input, aborting.")
        return

    values = [v.strip() for v in values_str.split(",")]

    print(f"\n{'amp':>10}{'in_pp':>12}{'out_pp':>12}{'out/in':>12}{'dB':>10}")
    print("-" * 56)

    import math
    base_ratio = None

    for v_str in values:
        text, ok = set_sin_amplitude(base_text, src_ref, v_str)
        if not ok:
            print(f"{v_str:>10}  could not find SIN source {src_ref}")
            continue

        out_path = os.path.join(tmpdir, f"amp_{v_str}.txt")
        cir_path = os.path.join(tmpdir, f"amp_{v_str}.cir")
        netlist = build_tran_netlist(text, [in_sig, out_sig], tstep, tstop, out_path)
        with open(cir_path, "w") as f:
            f.write(netlist)
        proc = run_ngspice(cir_path)
        if not os.path.exists(out_path):
            print(f"{v_str:>10}  ngspice run failed")
            continue

        data = read_wrdata(out_path, [in_sig, out_sig])
        times = data.get("time", [])
        if not times:
            print(f"{v_str:>10}  no data produced")
            continue
        tmax = max(times)
        t0, t1 = tmax * w0, tmax * w1
        idxs = [i for i, t in enumerate(times) if t0 <= t <= t1]
        in_series = data.get(in_sig.lower(), [])
        out_series = data.get(out_sig.lower(), [])
        if not in_series or not out_series:
            print(f"{v_str:>10}  signal not found in output")
            continue
        in_win = [in_series[i] for i in idxs]
        out_win = [out_series[i] for i in idxs]
        in_pp = max(in_win) - min(in_win)
        out_pp = max(out_win) - min(out_win)
        ratio = out_pp / in_pp if in_pp > 0 else float("nan")
        db = 20 * math.log10(ratio) if ratio > 0 else float("nan")

        if base_ratio is None and ratio > 0:
            base_ratio = ratio
        flag = ""
        if base_ratio and ratio > 0:
            dev_pct = abs(ratio - base_ratio) / base_ratio * 100
            if dev_pct > 10:
                flag = "  <-- deviates >10% from smallest-signal ratio"

        print(f"{v_str:>10}{in_pp:12.5f}{out_pp:12.5f}{ratio:12.4f}{db:10.2f}{flag}")


# --------------------------------------------------------------------------
# [8] Input/output impedance check
# --------------------------------------------------------------------------

def find_zero_ohm_group(text, node):
    """Follow 0-ohm resistor jumpers (RJPxx-style, value '0') to find every
    node electrically identical to `node`. Needed because a source tied to
    'JACK_IN' also effectively drives 'bf_in' if a 0-ohm jumper ties them
    together -- disabling sources by exact node-name match alone would miss
    that and give a falsely near-zero impedance reading."""
    adjacency = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.upper().startswith("R"):
            continue
        toks = line.split()
        if len(toks) < 4:
            continue
        try:
            val = float(toks[3])
        except ValueError:
            continue
        if val == 0:
            a, b = toks[1], toks[2]
            adjacency.setdefault(a.lower(), set()).add(b.lower())
            adjacency.setdefault(b.lower(), set()).add(a.lower())

    group = {node.lower()}
    stack = [node.lower()]
    while stack:
        n = stack.pop()
        for neighbor in adjacency.get(n, ()):
            if neighbor not in group:
                group.add(neighbor)
                stack.append(neighbor)
    return group


def disable_source_at_node(text, node):
    """Fully comment out any independent V/I source whose FIRST node
    matches `node` OR any node electrically identical to it via a 0-ohm
    jumper. Needed because zeroing a source's AC magnitude alone isn't
    enough when the queried node IS (or is jumpered to) that source's own
    output terminal -- an ideal source still presents 0-ohm impedance there
    regardless of its AC value, which would make the impedance measurement
    read near-zero no matter what's really downstream."""
    group = find_zero_ohm_group(text, node)
    new_text = text
    for n in group:
        def repl(m):
            return "*" + m.group(0)
        pattern = re.compile(rf'^([VI]\w+\s+){re.escape(n)}\s+.*$', re.MULTILINE | re.IGNORECASE)
        new_text = pattern.sub(repl, new_text)
    return new_text


def eval_impedance(base_text, tmpdir, cirfile):
    print("\n--- Input/output impedance check ---")
    print("Measures impedance looking into a node: injects a 1A AC test")
    print("current there (with all other AC sources zeroed) and reads the")
    print("resulting voltage magnitude, which equals the impedance in ohms.")
    labeled = extract_labeled_nodes(base_text)
    if labeled:
        print(f"Labeled nodes found: {' '.join(labeled)}")
    nodes_str = ask("Node(s) to check, space-separated (e.g. bf_in vca_out JACK_IN): ")
    nodes = nodes_str.split()
    if not nodes:
        print("No nodes given, aborting.")
        return
    freqs_str = ask("Frequencies to test in Hz, space-separated (default 100 1000 10000): ",
                     default="100 1000 10000")
    freqs = freqs_str.split()

    quiet_text = zero_all_ac_sources(base_text)

    print(f"\n{'Node':<15}", end="")
    for f in freqs:
        print(f"{f+' Hz':>14}", end="")
    print()
    print("-" * (15 + 14 * len(freqs)))

    for node in nodes:
        if node.upper() in ("GND", "0"):
            print(f"{node:<15}  (skipped: reference node, impedance is meaningless)")
            continue
        node_text = disable_source_at_node(quiet_text, node)
        row = f"{node:<15}"
        for f in freqs:
            control = f"""
Itest_{node} {node} 0 AC 1
.control
ac lin 1 {f} {f}
print vm({node})
.endc
"""
            idx = node_text.rstrip().rfind(".end")
            netlist = node_text[:idx] + control + "\n.end\n"
            cir_path = os.path.join(tmpdir, f"imp_{node}_{f}.cir")
            with open(cir_path, "w") as fh:
                fh.write(netlist)
            proc = run_ngspice(cir_path)
            m = re.search(r'vm\(' + re.escape(node.lower()) + r'\)\s*=\s*([\d.eE+-]+)', proc.stdout)
            if m:
                z = float(m.group(1))
                row += f"{z:12.1f} Ω"
            else:
                row += f"{'N/A':>14}"
        print(row)


# --------------------------------------------------------------------------
# [9] Pot rotation (POS) sweep
# --------------------------------------------------------------------------

def find_pot_instances(text):
    """Scan for X-instance lines that carry a POS=... parameter, i.e.
    instances of a rotation-aware pot subckt like POT_LOG/POT_LIN
    (as opposed to XPOT1-style instances that use a fixed POT_MODEL
    with no POS parameter at all)."""
    refs = []
    for line in text.splitlines():
        line = line.strip()
        if not line.upper().startswith("X"):
            continue
        if re.search(r'\bPOS\s*=', line):
            refs.append(line.split()[0])
    return refs


def replace_pot_pos(text, ref, pos_value):
    """Replace the POS=... value on a specific X<ref> pot instantiation line.
    Only touches that one instance line, so other pots (or other POS=
    occurrences) elsewhere in the file are left alone."""
    pattern = re.compile(
        rf'^({re.escape(ref)}\s.*?\bPOS\s*=\s*)\S+',
        re.MULTILINE | re.IGNORECASE
    )
    new_text, n = pattern.subn(rf'\g<1>{pos_value}', text)
    return new_text, n > 0


def eval_pot_sweep(base_text, tmpdir, cirfile):
    print("\n--- Pot rotation (POS) sweep ---")
    print("Sweeps a POT_LOG/POT_LIN instance's rotation (POS, 0=fully at A,")
    print("1=fully at B) and reports the resulting DC level of one or more")
    print("signals -- e.g. how the noise-gate threshold control voltage")
    print("(q21_c) moves as you turn the Threshold knob.")

    pot_refs = find_pot_instances(base_text)
    if pot_refs:
        print(f"Pot instances with a POS parameter found: {' '.join(pot_refs)}")
    ref = ask("Pot instance to sweep (e.g. XPOT2): ")
    if not ref:
        print("No pot instance given, aborting.")
        return

    values_str = ask(
        "POS values to try, comma-separated (default 0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0): ",
        default="0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0")
    values = [v.strip() for v in values_str.split(",")]

    print_node_suggestions(base_text, keyword="q21")
    signals_str = ask(
        "Signal(s) to report, space-separated (default q21_c): ",
        default="q21_c")
    signals = [ensure_v_wrap(s) for s in signals_str.split()]
    if not signals:
        print("No signals given, aborting.")
        return

    tstep = ask("Time step (default 10u): ", default="10u")
    tstop = ask("Final time (default 30m): ", default="30m")
    settle = float(ask("Fraction of the run to treat as 'settled' (default 0.5): ", default="0.5"))

    header = f"\n{'POS':>6}"
    for sig in signals:
        header += f"{sig + ' (mV)':>18}"
    print(header)
    print("-" * (6 + 18 * len(signals)))

    prev = {sig: None for sig in signals}

    for v_str in values:
        text, ok = replace_pot_pos(base_text, ref, v_str)
        if not ok:
            print(f"{v_str:>6}  could not find pot instance {ref}")
            continue

        out_path = os.path.join(tmpdir, f"potpos_{v_str}.txt")
        cir_path = os.path.join(tmpdir, f"potpos_{v_str}.cir")
        netlist = build_tran_netlist(text, signals, tstep, tstop, out_path)
        with open(cir_path, "w") as f:
            f.write(netlist)
        proc = run_ngspice(cir_path)
        if not os.path.exists(out_path):
            print(f"{v_str:>6}  ngspice run failed")
            continue

        data = read_wrdata(out_path, signals)
        times = data.get("time", [])
        if not times:
            print(f"{v_str:>6}  no data produced")
            continue
        tmax = max(times)
        window_start = tmax * (1 - settle)
        idxs = [i for i, t in enumerate(times) if t > window_start]

        row = f"{v_str:>6}"
        for sig in signals:
            series = data.get(sig.lower(), [])
            if not series:
                row += f"{'N/A':>18}"
                continue
            seg = [series[i] for i in idxs]
            dc_mv = sum(seg) / len(seg) * 1000
            delta = ""
            if prev[sig] is not None:
                delta = f" ({dc_mv - prev[sig]:+.2f})"
            row += f"{dc_mv:14.3f}{delta:>4}"
            prev[sig] = dc_mv
        print(row)


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------

MENU = {
    "1": ("Power consumption", eval_power),
    "2": ("DC offset check", eval_offset),
    "3": ("Gain / amplitude ratio", eval_gain),
    "4": ("Resistor divider sweep", eval_sweep),
    "5": ("Export .tran to CSV", eval_export),
    "6": ("Circuit noise analysis (.noise)", eval_noise),
    "7": ("Amplitude sweep (compression check)", eval_amp_sweep),
    "8": ("Input/output impedance check", eval_impedance),
    "9": ("Pot rotation (POS) sweep", eval_pot_sweep),
}


def banner():
    print("=" * 60)
    print(" SPICE netlist evaluation toolkit")
    print("=" * 60)
    print("Floating (unconnected) nodes are auto-patched with a 1G-ohm")
    print("resistor to GND so ngspice's operating point can converge.")
    print("Your original netlist file is never modified.")
    print("-" * 60)


def main():
    banner()
    cirfile = choose_netlist()
    print(f"\nSelected: {cirfile}\n")
    base_text = load_and_patch(cirfile)

    tmpdir = tempfile.mkdtemp(prefix="spice_eval_")

    while True:
        print("\nWhat would you like to evaluate?")
        for k, (label, _) in MENU.items():
            print(f"  [{k}] {label}")
        print("  [q] Quit")
        choice = ask("Pick one: ")
        if choice.lower() in ("q", "quit", ""):
            break
        entry = MENU.get(choice)
        if not entry:
            print("Invalid choice.")
            continue
        _, func = entry
        try:
            func(base_text, tmpdir, cirfile)
        except Exception as e:
            print(f"Error during evaluation: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
