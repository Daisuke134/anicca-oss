"""test_verify_loops_marker_list_and_cut_length.py — self-heal.md FIND-002 regression lock
(adversary finding on commit 69b48dae, sprint-1 iteration-1: root cause #2's visibility fix —
verify-loops.sh's self-fix marker loop expanded 3->10 loops, verify-loops-audit.sh's mail cut
900->3000 — shipped as two plain literal edits with zero regression coverage; a future edit could
silently drop a loop name or shrink the cut back down and nothing would catch it before the exact
same invisible-diagnosis failure mode root cause #2 already produced once).

Static source-fidelity check (mirrors this codebase's own test_cadence_contracts_gig_source.py
convention — "entry is byte-for-byte unchanged" against the REAL shipped file, not a duplicated
hardcoded copy elsewhere): reads the actual verify-loops.sh / verify-loops-audit.sh source and
asserts the exact loop-name list and cut-length are present, never re-derives them from memory.
"""
import os
import re
import sys

SELF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY_LOOPS_SH = os.path.join(SELF_DIR, "verify-loops.sh")
VERIFY_LOOPS_AUDIT_SH = os.path.join(SELF_DIR, "verify-loops-audit.sh")

P = 0
F = 0


def chk(name, got, want):
    global P, F
    if got == want:
        print(f"  ok {name} ({got})")
        P += 1
    else:
        print(f"  FAIL {name} want={want} got={got}")
        F += 1


with open(VERIFY_LOOPS_SH, encoding="utf-8") as f:
    verify_loops_src = f.read()

with open(VERIFY_LOOPS_AUDIT_SH, encoding="utf-8") as f:
    verify_loops_audit_src = f.read()

# --- self-fix marker loop must list exactly the canonical marker loops (self-heal.md root cause #2:
# previously only 3 of these were listed, hiding affiliate's #994/bounty's #995 diagnoses). ---
CANONICAL_MARKERS = [
    "clip-loop", "affiliate-loop", "gig-loop", "bounty-loop",
    "pm-earner-loop", "founder-loop", "capafy-loop", "reddit-loop", "life-manager-loop",
]

m = re.search(
    r'for L in ([\w\- ]+); do r="\$HOME/\.local/state/life-manager/state/\.self-fix-\$L\.result"',
    verify_loops_src,
)
chk("verify-loops.sh: self-fix marker loop line found (script structure unchanged)",
    m is not None, True)
if m:
    actual_list = m.group(1).split()
    chk("verify-loops.sh: self-fix marker loop lists every canonical marker, in order, no extras/omissions",
        actual_list, CANONICAL_MARKERS)

# --- mail-body cut length must be 3000, and the old 900 must be gone entirely (self-heal.md root
# cause #2: 900 truncated $OUT before the self-fix marker section it prints ever reached the mail). ---
chk("verify-loops-audit.sh: mail body uses cut -c1-3000",
    "cut -c1-3000" in verify_loops_audit_src, True)
chk("verify-loops-audit.sh: the old cut -c1-900 is fully gone (no silent shrink-back)",
    "cut -c1-900" in verify_loops_audit_src, False)

print(f"=== test_verify_loops_marker_list_and_cut_length: {P} passed {F} failed ===")
sys.exit(0 if F == 0 else 1)
