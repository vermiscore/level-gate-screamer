#!/usr/bin/env python3
"""
kicad2spice.py

Convert a KiCad .net (Eeschema S-expression netlist, with the built-in
"Sim.Device" / "Sim.Pins" / "Sim.Params" / "Sim.Library" / "Sim.Name"
simulation fields) into a flat, human-readable SPICE netlist similar in
spirit to an xschem-exported netlist.

Usage:
    python3 kicad2spice.py input.net output.spice

Design notes / assumptions (see chat for the reasoning):
  - Devices that carry Sim.* fields (R override, C override, D, NPN/PNP,
    V, SUBCKT ...) are converted automatically using the pin-mapping
    encoded in Sim.Pins.
  - Bare "R" / "C" refs with no Sim.* fields (plain resistors/caps) are
    converted using their reference prefix + Value field.
  - Parts with no simulation meaning (audio jacks, jumpers, footswitches,
    battery) are NOT auto-wired together -- they are emitted as comments
    listing which nets they touch, so you can decide by hand how to model
    the switch position / power source, instead of the script silently
    guessing and producing a wrong topology.
  - Net names are kept as close to the original KiCad net name as
    possible; only characters that are not legal in a SPICE node name
    (parentheses, commas, spaces) are stripped/replaced.
"""

import sys
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Minimal S-expression parser (stdlib only, no external dependency)
# ---------------------------------------------------------------------------

def tokenize(text):
    """Split KiCad's s-expression text into tokens: '(', ')', quoted strings,
    or bare atoms."""
    token_re = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+')
    for m in token_re.finditer(text):
        tok = m.group(0)
        if tok.startswith('"') and tok.endswith('"'):
            tok = tok[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        yield tok


def parse_sexp(text):
    """Parse into nested python lists: (a (b c) d) -> ['a', ['b', 'c'], 'd']"""
    tokens = list(tokenize(text))
    pos = 0

    def parse():
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == '(':
            node = []
            while tokens[pos] != ')':
                node.append(parse())
            pos += 1  # consume ')'
            return node
        else:
            return tok

    result = parse()
    return result


def find_all(node, tag):
    """Find all direct child lists of `node` whose first element == tag."""
    out = []
    if isinstance(node, list):
        for child in node:
            if isinstance(child, list) and child and child[0] == tag:
                out.append(child)
    return out


def find_first(node, tag):
    r = find_all(node, tag)
    return r[0] if r else None


def leaf_value(node, tag):
    """For a child shaped like (tag "value"), return "value"."""
    child = find_first(node, tag)
    if child is None:
        return None
    if len(child) >= 2:
        return child[1]
    return None


# ---------------------------------------------------------------------------
# 2. Extract components and nets from the parsed tree
# ---------------------------------------------------------------------------

def parse_pin_map(s):
    """'1=K 2=A' -> {'1': 'K', '2': 'A'}   or   '1=5 2=2 3=1 4=4 8=3' -> {...}"""
    result = {}
    if not s:
        return result
    for part in s.split():
        if '=' in part:
            k, v = part.split('=', 1)
            result[k] = v
    return result


def parse_params(s):
    """'dc=0 ampl=0.5 f=1k td=0 theta=0 phase=0 ac=1' -> dict"""
    result = {}
    if not s:
        return result
    for part in s.split():
        if '=' in part:
            k, v = part.split('=', 1)
            result[k] = v
    return result


def extract_components(tree):
    comps_node = find_first(tree, 'components')
    comps = {}
    for comp in find_all(comps_node, 'comp'):
        ref = leaf_value(comp, 'ref')
        value = leaf_value(comp, 'value') or ''

        props = {}
        for prop in find_all(comp, 'property'):
            # (property (name "X") (value "Y"))
            name = leaf_value(prop, 'name')
            val = leaf_value(prop, 'value')
            if name is not None:
                props[name] = val

        comps[ref] = {
            'ref': ref,
            'value': value,
            'sim_device': props.get('Sim.Device'),
            'sim_type': props.get('Sim.Type'),
            'sim_pins': parse_pin_map(props.get('Sim.Pins')),
            'sim_params': parse_params(props.get('Sim.Params')),
            'sim_library': props.get('Sim.Library'),
            'sim_name': props.get('Sim.Name'),
        }
    return comps


def sanitize_net(name):
    """Make a KiCad net name safe as a SPICE node name while keeping it as
    recognizable as possible."""
    n = name
    n = n.replace('(', '').replace(')', '')
    n = n.replace(',', '_').replace(' ', '_')
    return n


def extract_nets(tree):
    """Returns node_of[(ref, pin)] = sanitized_net_name"""
    nets_node = find_first(tree, 'nets')
    node_of = {}
    for net in find_all(nets_node, 'net'):
        net_name = leaf_value(net, 'name') or ('N' + (leaf_value(net, 'code') or '?'))
        net_name = sanitize_net(net_name)
        for node in find_all(net, 'node'):
            ref = leaf_value(node, 'ref')
            pin = leaf_value(node, 'pin')
            node_of[(ref, pin)] = net_name
    return node_of


# ---------------------------------------------------------------------------
# 3. Terminal ordering tables for named (non-numeric) Sim.Pins targets
# ---------------------------------------------------------------------------

NAMED_ORDER = {
    'D':   ['A', 'K'],            # SPICE diode: anode, cathode
    'NPN': ['C', 'B', 'E'],       # SPICE BJT: collector, base, emitter
    'PNP': ['C', 'B', 'E'],
    'R':   ['+', '-'],
    'C':   ['+', '-'],
    'L':   ['+', '-'],
    'V':   ['+', '-'],
    'I':   ['+', '-'],
}


def ordered_nets_for(comp, node_of):
    """Return the list of net names in the correct SPICE pin order for a
    component that carries Sim.Pins, using its Sim.Device to know whether
    the mapping targets are named terminals (A/K, C/B/E, +/-) or numeric
    subckt pin indices."""
    ref = comp['ref']
    device = comp['sim_device']
    pin_map = comp['sim_pins']  # symbol_pin -> target

    targets = set(pin_map.values())
    numeric = all(t.lstrip('-').isdigit() for t in targets)

    if numeric:
        # SUBCKT-style: order by ascending target (model pin) index
        order = sorted(pin_map.items(), key=lambda kv: int(kv[1]))
    else:
        wanted_order = NAMED_ORDER.get(device)
        if wanted_order is None:
            # Unknown named convention: fall back to symbol pin ascending
            order = sorted(pin_map.items(), key=lambda kv: int(kv[0]))
        else:
            by_target = {v: k for k, v in pin_map.items()}
            order = [(by_target[t], t) for t in wanted_order if t in by_target]
            # append anything not covered by the known order, just in case
            covered = {t for _, t in order}
            for k, v in pin_map.items():
                if v not in covered:
                    order.append((k, v))

    nets = []
    for symbol_pin, _target in order:
        key = (ref, symbol_pin)
        nets.append(node_of.get(key, f'??{ref}_pin{symbol_pin}'))
    return nets


# ---------------------------------------------------------------------------
# 4. Emit one SPICE line per component
# ---------------------------------------------------------------------------

def natural_key(ref):
    m = re.match(r'([A-Za-z_]+)(\d+)', ref)
    if m:
        return (m.group(1), int(m.group(2)))
    return (ref, 0)


def ref_num(ref):
    """Trailing numeric part of a reference designator: 'POT1' -> '1',
    'R12' -> '12'."""
    m = re.search(r'(\d+)$', ref)
    return m.group(1) if m else ref


def spice_name(device_letter, ref):
    """Build the SPICE element name. If the original KiCad reference
    already starts with the required SPICE type letter (R1, C3, D2, Q1,
    V2 ...) just reuse its number, so the SPICE name stays short and
    matches the schematic reference. Otherwise (e.g. a potentiometer
    "POT1" being simulated as a plain resistor, device_letter 'R') keep
    the whole original ref appended, to avoid colliding with a real R1/R2/...
    """
    if ref.upper().startswith(device_letter.upper()):
        return device_letter + ref_num(ref)
    return device_letter + ref


def build_spice_lines(comps, node_of):
    passive_lines = []
    semi_lines = []
    opamp_lines = []
    source_lines = []
    manual_lines = []
    lib_includes = set()
    model_todo = set()

    for ref in sorted(comps.keys(), key=natural_key):
        comp = comps[ref]
        device = comp['sim_device']
        value = comp['value']

        if device is None:
            # No simulation annotation at all.
            if ref.startswith('R'):
                pins = sorted([p for (r, p) in node_of if r == ref])
                nets = [node_of[(ref, p)] for p in pins]
                if len(nets) == 2:
                    passive_lines.append(f'R{ref_num(ref)} {nets[0]} {nets[1]} {value}')
                continue
            if ref.startswith('C') and not ref.startswith('CC'):
                pins = sorted([p for (r, p) in node_of if r == ref])
                nets = [node_of[(ref, p)] for p in pins]
                if len(nets) == 2:
                    passive_lines.append(f'C{ref_num(ref)} {nets[0]} {nets[1]} {value}')
                continue

            # Everything else without Sim.* info: no safe automatic
            # conversion -- list its net connections as a comment so the
            # user can decide how to model it (switch position, which
            # power source is active, jack normalled contacts, etc).
            pins = sorted([p for (r, p) in node_of if r == ref], key=lambda p: (len(p), p))
            conn = ', '.join(f'pin{p}={node_of[(ref, p)]}' for p in pins)
            manual_lines.append(f'* {ref} ({value}) -- not auto-converted: {conn}')
            continue

        nets = ordered_nets_for(comp, node_of)

        if device == 'D':
            model = value or ref
            semi_lines.append(f"{spice_name('D', ref)} {nets[0]} {nets[1]} {model}")
            model_todo.add(model)

        elif device in ('NPN', 'PNP'):
            model = value or ref
            semi_lines.append(f"{spice_name('Q', ref)} {nets[0]} {nets[1]} {nets[2]} {model}")
            model_todo.add(model)

        elif device == 'R':
            # Potentiometer etc. simulated as fixed R at Sim.Params r=...
            r_val = comp['sim_params'].get('r', value)
            passive_lines.append(f"{spice_name('R', ref)} {nets[0]} {nets[1]} {r_val}")

        elif device == 'C':
            c_val = comp['sim_params'].get('c', value)
            passive_lines.append(f"{spice_name('C', ref)} {nets[0]} {nets[1]} {c_val}")

        elif device == 'L':
            l_val = comp['sim_params'].get('l', value)
            passive_lines.append(f"{spice_name('L', ref)} {nets[0]} {nets[1]} {l_val}")

        elif device == 'V':
            p = comp['sim_params']
            sim_type = (comp['sim_type'] or '').upper()
            if sim_type == 'SIN':
                dc = p.get('dc', '0')
                ampl = p.get('ampl', '0')
                f = p.get('f', '1k')
                td = p.get('td', '0')
                theta = p.get('theta', '0')
                phase = p.get('phase', '0')
                ac = p.get('ac', '0')
                line = (f"{spice_name('V', ref)} {nets[0]} {nets[1]} DC {dc} AC {ac} "
                        f"SIN({dc} {ampl} {f} {td} {theta} {phase})")
            else:
                # plain DC source
                dc = value or p.get('dc', '0')
                line = f"{spice_name('V', ref)} {nets[0]} {nets[1]} DC {dc}"
            source_lines.append(line)

        elif device == 'SUBCKT':
            subckt_name = comp['sim_name'] or value or ref
            lib = comp['sim_library']
            if lib:
                lib_includes.add(lib)
            opamp_lines.append(f'{spice_name("X", ref)} {" ".join(nets)} {subckt_name}')

        else:
            conn = ' '.join(nets)
            manual_lines.append(f'* {ref}: unrecognized Sim.Device="{device}", '
                                 f'nets in Sim.Pins order: {conn}')

    return {
        'passive': passive_lines,
        'semi': semi_lines,
        'opamp': opamp_lines,
        'source': source_lines,
        'manual': manual_lines,
        'lib_includes': sorted(lib_includes),
        'model_todo': sorted(model_todo),
    }


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def convert(in_path, out_path):
    text = Path(in_path).read_text(encoding='utf-8')
    tree = parse_sexp(text)

    design = find_first(tree, 'design')
    source = leaf_value(design, 'source') if design else None

    comps = extract_components(tree)
    node_of = extract_nets(tree)
    groups = build_spice_lines(comps, node_of)

    out = []
    out.append(f'* SPICE netlist auto-generated from KiCad netlist: {Path(in_path).name}')
    if source:
        out.append(f'* Source schematic: {source}')
    out.append('* Generated by kicad2spice.py')
    out.append('')

    if groups['lib_includes'] or groups['model_todo']:
        out.append('** models / library includes -- fill in real paths **')
        for lib in groups['lib_includes']:
            out.append(f'.include {lib}')
        if groups['model_todo']:
            out.append('* TODO: provide SPICE .model definitions (or vendor libs) for:')
            out.append('*   ' + ', '.join(groups['model_todo']))
        out.append('')

    if groups['source']:
        out.append('** sources **')
        out.extend(groups['source'])
        out.append('')

    if groups['passive']:
        out.append('** passives (R, C, L, pots) **')
        out.extend(groups['passive'])
        out.append('')

    if groups['semi']:
        out.append('** diodes / transistors **')
        out.extend(groups['semi'])
        out.append('')

    if groups['opamp']:
        out.append('** op-amp subcircuits **')
        out.extend(groups['opamp'])
        out.append('')

    if groups['manual']:
        out.append('** parts needing manual attention (connectors, switch, battery) **')
        out.append('* These have no simulation meaning by themselves; wire them in')
        out.append('* by hand according to the footswitch position / power source you')
        out.append('* want to simulate.')
        out.extend(groups['manual'])
        out.append('')

    out.append('.end')

    Path(out_path).write_text('\n'.join(out) + '\n', encoding='utf-8')
    return out_path


def pick_input_interactively():
    """List *.net files in the current directory and let the user pick one
    by number. Returns the chosen path as a string, or None if cancelled."""
    candidates = sorted(Path('.').glob('*.net'))
    if not candidates:
        path = input('Path to KiCad netlist (.net): ').strip()
        return path or None

    print('.net files found in current directory:')
    for i, p in enumerate(candidates, 1):
        print(f'  [{i}] {p.name}')
    print(f'  [0] enter a different path')

    choice = input(f'Choose a number (1-{len(candidates)}, 0, Enter for 1): ').strip()
    if choice == '':
        choice = '1'
    if choice == '0':
        path = input('Path: ').strip()
        return path or None
    try:
        idx = int(choice)
        if 1 <= idx <= len(candidates):
            return str(candidates[idx - 1])
    except ValueError:
        pass
    print('Invalid choice.')
    return None


def pick_output_interactively(in_path):
    default_out = str(Path(in_path).with_suffix('.spice'))
    out = input(f'Output filename (Enter for {default_out}): ').strip()
    return out or default_out


if __name__ == '__main__':
    if len(sys.argv) == 3:
        in_path, out_path = sys.argv[1], sys.argv[2]
    elif len(sys.argv) == 1:
        in_path = pick_input_interactively()
        if not in_path or not Path(in_path).is_file():
            print(f'File not found: {in_path}')
            sys.exit(1)
        out_path = pick_output_interactively(in_path)
    else:
        print('Usage: python3 kicad2spice.py [<input.net> <output.spice>]')
        print('       (run with no arguments for interactive file selection)')
        sys.exit(1)

    convert(in_path, out_path)
    print(f'Wrote {out_path}')
