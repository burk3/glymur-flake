"""a16-snap: standard read-only diagnostic snapshots of the A16 (docs/a16-snap.md).

The A16 side (`a16-snap`, snap.main) collects; the observer host's `a16 snap` runs it
over ssh and keeps the results; both share the store and diff code.
"""

# Bump when collectors or the snapshot layout change: the observer host's `a16 snap`
# compares it with the A16's installed a16-snap and copies its own build over
# when they differ.
SNAP_VERSION = "1.2"
