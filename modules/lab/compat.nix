{ lib, ... }:

let
  alias =
    from: to:
    lib.mkAliasOptionModule
      (
        [
          "lab"
          "zenbook"
        ]
        ++ from
      )
      (
        [
          "hardware"
          "asus-zenbook-a16"
        ]
        ++ to
      );
in
{
  imports = [
    (alias [ "kernel" ] [ "kernel" "variant" ])
    (alias [ "kernelConfig" ] [ "kernel" "config" ])
    (alias [ "kernelCcache" "enable" ] [ "kernel" "ccache" "enable" ])
    (alias [ "kernelCcache" "dir" ] [ "kernel" "ccache" "dir" ])
    (alias [ "firmware" "enable" ] [ "firmware" "enable" ])
    (alias [ "firmware" "dsp" "extraInitrdModules" ] [ "firmware" "dsp" "extraInitrdModules" ])
    (alias [ "wifi" "unloadAcrossSuspend" ] [ "wifi" "unloadAcrossSuspend" ])
    (alias [ "edpRetrain" "enable" ] [ "edpRetrain" "enable" ])
    (alias [ "irEmitter" "stage" ] [ "irEmitter" "stage" ])
    (alias [ "howdy" "enable" ] [ "howdy" "enable" ])
    (alias [ "howdy" "sddm" ] [ "howdy" "sddm" ])
    (alias [ "howdy" "pamServices" ] [ "howdy" "pamServices" ])
  ];
}
