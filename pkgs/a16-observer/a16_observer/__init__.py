"""A16 observability (docs/observability-plan.md).

the observer host side (in the a16-obs container): the a16-serial daemon, the heartbeat,
netconsole and mark sinks (`a16-obs <daemon>`). A16 side: the heartbeat sender,
the node_exporter textfile collector and the boot record with the PMIC PON
dump (`a16-telemetry <cmd>`). Both: the `a16` CLI. Standard library only.
"""

__version__ = "0.1.0"
