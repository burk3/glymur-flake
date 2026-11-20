# Patch queue policy, evaluated by `nix flake check` (checks.patch-manifest)
# and usable from other flakes: import it with { lib; } and read `errors`.
#   - every kernel/patches/*.patch has a manifest entry, and every entry's
#     file exists; every variant's patches are in the manifest;
#   - every entry has a licence;
#   - vendored notForUpstream patches carry the subject prefix and trailer;
#   - no forbidden Signed-off-by (someone's sign-off added for them) unless
#     the patch is listed in signoffAllowed.
{ lib }:
let
  manifest = lib.importJSON ./manifest.json;
  policy = lib.importJSON ./policy.json;
  labPatches = lib.importJSON ./lab-patches.json;
  dir = ./patches;
  files = lib.concatLists (
    lib.mapAttrsToList (
      n: t:
      if t == "directory" then
        map (m: "${n}/${m}") (lib.attrNames (lib.filterAttrs (m: _: lib.hasSuffix ".patch" m) (builtins.readDir (dir + "/${n}"))))
      else
        lib.optional (lib.hasSuffix ".patch" n) n
    ) (builtins.readDir dir)
  );
  vendored = lib.filterAttrs (_: e: e ? file) manifest;
  byFile = lib.listToAttrs (lib.mapAttrsToList (n: e: lib.nameValuePair e.file n) vendored);
  variants = lib.filterAttrs (n: t: t == "regular" && lib.hasSuffix ".json" n) (builtins.readDir ./variants);
  text = e: builtins.readFile (dir + "/${e.file}");
  subjectOf = t: lib.findFirst (l: lib.hasPrefix "Subject:" l) "" (lib.splitString "\n" t);
  errors =
    map (f: "kernel/patches/${f}: no manifest entry") (lib.filter (f: !(byFile ? ${f})) files)
    ++ lib.concatLists (
      lib.mapAttrsToList (
        n: e:
        let
          t = text e;
        in
        lib.optional (!(lib.elem e.file files)) "${n}: kernel/patches/${e.file} missing"
        ++ lib.optional (lib.elem e.file files && e.notForUpstream && !(lib.hasInfix policy.subjectPrefix (subjectOf t))) "${n}: notForUpstream but no '${policy.subjectPrefix}' subject prefix"
        ++ lib.optional (lib.elem e.file files && e.notForUpstream && !(lib.hasInfix policy.trailer t)) "${n}: notForUpstream but no Not-for-upstream trailer"
        ++ lib.optionals (lib.elem e.file files && !(lib.elem n policy.signoffAllowed)) (
          map (who: "${n}: Signed-off-by: ${who} (not added by them)") (
            lib.filter (who: lib.hasInfix "Signed-off-by: ${who}" t) policy.forbiddenSignoffs
          )
        )
      ) vendored
    )
    ++ lib.concatLists (lib.mapAttrsToList (n: e: lib.optional ((e.license or null) == null) "${n}: no license") manifest)
    ++ lib.concatLists (
      lib.mapAttrsToList (
        n: e:
        lib.optional ((e.labOption or null) != null && !(labPatches ? ${e.labOption})) "${n}: labOption ${e.labOption} not in lab-patches.json"
        ++ lib.optional ((e.status or "") == "lab" && !(e.notForUpstream or false)) "${n}: status lab but not marked notForUpstream"
      ) manifest
    )
    ++ lib.concatLists (
      lib.mapAttrsToList (
        v: _:
        map (n: "${v}: ${n} not in manifest.json") (
          lib.filter (n: !(manifest ? ${n})) (lib.importJSON (./variants + "/${v}")).patches
        )
      ) variants
    );
in
{
  inherit errors manifest;
  summary = {
    patches = lib.length (lib.attrNames manifest);
    vendored = lib.length (lib.attrNames vendored);
    notForUpstream = lib.length (lib.filter (e: e.notForUpstream) (lib.attrValues manifest));
  };
}
