{
  lib,
  writeShellApplication,
  coreutils,
  util-linux,
  procps,
  # Lease names; null: LAB_LEASE_RESOURCES or the script's default.
  leaseResources ? null,
}:

writeShellApplication {
  name = "lab-lease";
  runtimeInputs = [
    coreutils
    util-linux
    procps
  ];
  bashOptions = [ ];
  text =
    lib.optionalString (leaseResources != null) ''
      : "''${LAB_LEASE_RESOURCES:=${lib.concatStringsSep " " leaseResources}}"
      export LAB_LEASE_RESOURCES
    ''
    + builtins.readFile ./lab-lease;
}
