# Kernel patch queue

A bring-up snapshot: the patches this flake applies on top of the kernel
release named in `kernel/variants/<variant>.json`, in that order. It is not a
submission. Patches posted upstream are carried unmodified or as
backports; take them from their upstream posting, by Message-ID, not
from here. Each patch keeps its author's From and Signed-off-by lines
and is GPL-2.0-only, as the kernel. `manifest.json` has per-patch
metadata (author, licence, source, status, Kconfig).

Patches marked `NOT-FOR-UPSTREAM:` (subject) with a `Not-for-upstream:`
note under `---` are local workarounds or experiments for testing on
this laptop only.

Patches referenced from FixItFoundry's zenbook-a16-linux are fetched from
that repository, not copied here.

## Upstream postings and commits

| Patch | Kind | Message-ID / commit |
|---|---|---|
| `0002-phy-qcom-edp-split-power-on-sequencing-by-PHY-versio` | upstream-posting, pending | (source to be filled in) |
| `0003-clk-qcom-gcc-glymur-Use-PWRSTS_RET_ON-flag-for-USB-GDSCs` | upstream-posting, pending | https://lore.kernel.org/all/20261016-glymur-usb-ret-on-v1-1-1790a523fd9c@oss.qualcomm.com/ |
| `0003-phy-qcom-edp-update-v8-power-on-programming-sequence` | upstream-posting, pending | (source to be filled in) |
| `0004-phy-qualcomm-phy-qcom-eusb2-repeater-Fix-SMB-Power-leakage` | upstream-posting, pending | https://lore.kernel.org/all/20260917-smb-repeater-power-leakage-v1-1-6d7311c3c03e@oss.qualcomm.com/ |
| `0005-usb-typec-ucsi-ucsi_glink-Prevent-suspend-during-UCSI-notification` | upstream-posting, pending | https://lore.kernel.org/all/20260710-ucsi_glink_wakeup-v1-1-7d97ea628d92@oss.qualcomm.com/ |
| `0006-usb-dwc3-qcom-Add-support-to-skip-phy-management-by-USB-core` | upstream-posting, pending | https://lore.kernel.org/all/20260914-xhci-skip-phy-init-v1-1-9c46b31a5a39@oss.qualcomm.com/ |
| `0007-arm64-dts-qcom-glymur-Fix-missing-SMMU-stream-IDs-for-ADSP` | upstream-posting, pending | https://lore.kernel.org/lkml/20260811-adsp-smmu-fix-v1-1-a596d284c455@oss.qualcomm.com/ |
| `0008-clk-qcom-add-Glymur-LPASS-clock-controller-with-reset-lines` | upstream-posting, pending | (source to be filled in) |
| `0009-ASoC-codecs-lpass-macro-add-codec-version-2.9-and-fix-regcache` | upstream-posting, pending | (source to be filled in) |
| `0010-ASoC-qcom-lpass-macro-common-Add-missing-v2.9-version-string` | upstream-posting, pending | https://patch.msgid.link/20260922-add_shikra_support-v1-2-6c6cb6444caf@oss.qualcomm.com |
| `0011-ASoC-codecs-lpass-rx-macro-manage-clocks-in-runtime-PM` | upstream-posting, pending | https://lore.kernel.org/all/20260928-xo-sd-runtimepm-v1-final-v1-1-e3ebade3aeb7@oss.qualcomm.com/ |
| `0012-ASoC-codecs-lpass-tx-macro-manage-clocks-in-runtime-PM` | upstream-posting, pending | https://lore.kernel.org/all/20260928-xo-sd-runtimepm-v1-final-v1-2-e3ebade3aeb7@oss.qualcomm.com/ |
| `0013-ASoC-codecs-lpass-va-macro-manage-clocks-in-runtime-PM` | upstream-posting, pending | https://lore.kernel.org/all/20260928-xo-sd-runtimepm-v1-final-v1-3-e3ebade3aeb7@oss.qualcomm.com/ |
| `0014-ASoC-codecs-lpass-wsa-macro-manage-clocks-in-runtime-PM` | upstream-posting, pending | https://lore.kernel.org/all/20260928-xo-sd-runtimepm-v1-final-v1-4-e3ebade3aeb7@oss.qualcomm.com/ |
| `0015-ASoC-codecs-lpass-macro-drop-PM_CLK-dependency` | upstream-posting, pending | https://lore.kernel.org/all/20260928-xo-sd-runtimepm-v1-final-v1-5-e3ebade3aeb7@oss.qualcomm.com/ |
| `0016-power-supply-qcom_battmgr-Add-multi-port-USB-C-and-capacity` | upstream-posting, pending | https://lore.kernel.org/all/20260806-b4-battmgr-multiport-usb-v2-1-1a6dd1e06cc2@oss.qualcomm.com/ |
| `0017-dt-bindings-soc-qcom-pmic-glink-Document-RTC-client-properties` | upstream-posting, pending | https://lore.kernel.org/all/20260807-rtc-glink-cleanup-v1-1-7801e4d69c4f@oss.qualcomm.com/ |
| `0018-rtc-qcom-rtc-glink-Add-PMIC-GLINK-RTC-driver` | upstream-posting, pending | https://lore.kernel.org/all/20260807-rtc-glink-cleanup-v1-2-7801e4d69c4f@oss.qualcomm.com/ |
| `0021-drm-msm-dp-wake-threaded-handler-for-HPD-IRQs` | upstream-posting, pending | https://lore.kernel.org/all/20260929-msm-dp-mst-v7-33-4b81473185e7@oss.qualcomm.com/ |
| `0022-drm-msm-dp-skip-LTTPR-init-when-no-LTTPRs-are-present` | upstream-posting, pending | https://lore.kernel.org/all/20260824-qcom-dp-typec-reconnect-fixes-v1-3-2825e5bf8a96@oss.qualcomm.com/ |
| `0023-arm64-dts-qcom-glymur-Fix-fastrpc-compute-cb-reg-values-for-cdsp` | upstream-posting, pending | https://lore.kernel.org/all/20260923-glymur-dtfix-v2-1-a4b1928e0c72@oss.qualcomm.com/ |
| `0024-arm64-dts-qcom-glymur-Add-iris-video-node` | upstream-posting, pending | (source to be filled in) |
| `0024-dt-bindings-embedded-controller-Add-ASUS-Zenbook-A16` | upstream-posting, pending | (source to be filled in) |
| `0026-arm64-dts-qcom-glymur-zenbook-a16-Add-Embedded-Contr` | upstream-posting, pending | (source to be filled in) |
| `0026-wifi-ath12k-Fix-missing-station-power-save-configuration` | upstream-posting, pending | https://lore.kernel.org/all/20250908015025.1301398-1-miaoqing.pan@oss.qualcomm.com/ |
| `0027-PCI-qcom-Disable-halting-feature` | upstream-posting, pending | https://patch.msgid.link/20261009-halt-v1-1-4a2d7899d91f@oss.qualcomm.com |
| `0028-arch_topology-seed-capacity_freq_ref-with-boost-aware-max-freq` | upstream-posting, pending | https://lore.kernel.org/all/20260908-schedutil-boost-frequency-handling-v2-1-25312a713699@oss.qualcomm.com/ |
| `0029-arm64-dts-qcom-glymur-Add-EC-thermal-zones` | upstream-posting, pending | https://lore.kernel.org/all/20260928-ec_thermistor_support-v1-3-d9c1bae4cd0d@oss.qualcomm.com/ |
| `0030-tee-qcomtee-Track-the-object-invocation-context` | upstream-posting, pending | https://lore.kernel.org/lkml/20261006-qcom_uefisecapp_migrate_qcomtee-v4-1-bf1c8e2a64ab@oss.qualcomm.com/ |
| `0031-tee-Add-kernel-client-object-invoke-helper` | upstream-posting, pending | https://lore.kernel.org/lkml/20261006-qcom_uefisecapp_migrate_qcomtee-v4-2-bf1c8e2a64ab@oss.qualcomm.com/ |
| `0032-tee-qcomtee-Allow-object-invokes-from-kernel-clients` | upstream-posting, pending | https://lore.kernel.org/lkml/20261006-qcom_uefisecapp_migrate_qcomtee-v4-3-bf1c8e2a64ab@oss.qualcomm.com/ |
| `0033-tee-Export-uuidv5-generation-for-TEE-backends` | upstream-posting, pending | https://lore.kernel.org/lkml/20261006-qcom_uefisecapp_migrate_qcomtee-v4-4-bf1c8e2a64ab@oss.qualcomm.com/ |
| `0034-tee-qcomtee-Add-support-for-registering-QTEE-services-on-TEE-bus` | upstream-posting, pending | https://lore.kernel.org/lkml/20261006-qcom_uefisecapp_migrate_qcomtee-v4-5-bf1c8e2a64ab@oss.qualcomm.com/ |
| `0035-firmware-qcom-Add-support-for-TEE-based-EFI-var-client-driver` | upstream-posting, pending | https://lore.kernel.org/lkml/20261006-qcom_uefisecapp_migrate_qcomtee-v4-6-bf1c8e2a64ab@oss.qualcomm.com/ |
| `0036-tee-qcomtee-Register-qcom.tz.tpm-service-for-discovery` | upstream-posting, pending | https://lore.kernel.org/lkml/20260907-tpm_qcom_driver-v2-1-71a6b1752da8@oss.qualcomm.com/ |
| `0037-tpm-Introduce-Qualcomm-TPM-driver` | upstream-posting, pending | https://lore.kernel.org/lkml/20260907-tpm_qcom_driver-v2-2-71a6b1752da8@oss.qualcomm.com/ |
| `0038-arm64-dts-qcom-glymur-Add-CAMSS-and-CSIPHY-nodes-for-Glymur` | upstream-posting, pending | https://lore.kernel.org/all/20260907-glymur_camss_dts-v1-1-6c2ced5ea5f9@oss.qualcomm.com/ |
| `0039-arm64-dts-qcom-glymur-Add-CCI-definitions-for-Glymur` | upstream-posting, pending | https://lore.kernel.org/all/20260907-glymur_camss_dts-v1-2-6c2ced5ea5f9@oss.qualcomm.com/ |
| `0040-arm64-dts-qcom-glymur-Add-camera-MCLK-pinctrl` | upstream-posting, pending | https://lore.kernel.org/all/20260907-glymur_camss_dts-v1-3-6c2ced5ea5f9@oss.qualcomm.com/ |
| `arm64-dts-glymur-asus-zenbook-drop-cma-region-v1` | upstream-posting, pending | <20261014-dts-qcom-glymur-asus-drop-cma-v1-1-46f745d5b2d9@oss.qualcomm.com> |
| `arm64-dts-glymur-snps-resume-hs-terminations-v2-1` | upstream-posting, pending | <20261019021547.1694088-2-peter.chen@oss.qualcomm.com> |
| `ath12k-lli-v4-0001` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0002` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0003` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0004` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0005` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0006` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0007` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0008` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0009` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0010` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `ath12k-lli-v4-0011` | upstream-posting, pending | <20261102-ath12k-logical-link-id-v4-N-f4c7807c299b@oss.qualcomm.com> |
| `camera-0009-media-dt-bindings-Add-Himax-HM1092-NIR-sensor` | upstream-posting, pending | https://patch.msgid.link/20261013-hm1092-driver-v7-1-d81a9b152ffd@gmail.com |
| `camera-0010-media-i2c-hm1092-add-Himax-HM1092-mono-NIR-sensor-dr` | upstream-posting, pending | https://lore.kernel.org/r/20260727131252.1580332-1-j@metarealtyinc.ca |
| `camera-0011-media-i2c-hm1092-Add-ACPI-enumeration-and-the-19.2-M` | upstream-posting, pending | https://lore.kernel.org/r/b0d32a3b82eca233652f878b7ca91e4dc2d8e863.1791608336.git.j@metarealtyinc.ca |
| `drm-msm-dp-hdr10-v2-11-cache-edp-link-caps` | upstream-posting, pending | <20261009-msm-dp-hdr10-v2-11-1835d4966da3@radxa.com> |
| `glymur-phy-v6-01` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-02` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-03` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-04` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-05` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-06` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-07` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-08` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-09` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `glymur-phy-v6-10` | upstream-posting, pending | <20261028-glymur-phy-v3-v6-N-1f1d2e565a66@oss.qualcomm.com> |
| `k74-jack-0001` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0002` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0003` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0004` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0005` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0006` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0007` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0008` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0009` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0010` | upstream-posting, pending | https://patch.msgid.link/20261001093806.634862-2-srinivas.kandagatla@oss.qualcomm.com |
| `k74-jack-0011` | upstream-posting, pending | https://patch.msgid.link/20261001093806.634862-3-srinivas.kandagatla@oss.qualcomm.com |
| `k74-jack-0012` | upstream-posting, pending | https://patch.msgid.link/20261001093806.634862-4-srinivas.kandagatla@oss.qualcomm.com |
| `k74-jack-0013` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0014` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0015` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0016` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0017` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0018` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0019` | upstream-posting, pending | (source to be filled in) |
| `k74-jack-0021` | upstream-posting, pending | (source to be filled in) |
| `lpass-lpi-system-sleep-v2-1` | upstream-posting, pending | (source to be filled in) |
| `lpass-lpi-system-sleep-v2-2` | upstream-posting, pending | (source to be filled in) |
| `phy-qcom-qmp-combo-glymur-usb3-settings-konrad` | upstream-posting, pending | <76e97352-3a54-40de-8ef0-eef0ca27ca73@oss.qualcomm.com> |
| `pm8010-v3-1` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `pm8010-v3-2` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `pm8010-v3-3` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `pm8010-v3-4` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `pm8010-v3-5` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `pm8010-v3-6` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `pm8010-v3-7` | upstream-posting, pending | <20261030-pm8010_i2c_support-v3-N-995ed1ec7a72@oss.qualcomm.com> |
| `soundwire-qcom-disable-irq-before-gating-clock-v2` | upstream-posting, pending | <20261009082639.22662-1-okerixx@gmail.com> |
| `soundwire-qcom-system-sleep-v1-1` | upstream-posting, pending | (source to be filled in) |

## Local (68)

- `0001-drm-msm-dp-make-the-eDP-1.4-LINK_RATE_SET-path-actua`
- `0001-soundwire-qcom-allow-deferred-codec-probes`
- `0004-HID-asus-complete-the-feature-set-for-the-Zenbook-I2`
- `0005-wifi-carry-proven-A16-QCC2072-baseline-into-audio-te`
- `0010-LOCAL-EXPERIMENT-preserve-test4-SoundWire-compatibil`
- `0012-ASoC-qdsp6-q6apm-fast-retry-APM_CMD_GET_SPF_STATE-to`
- `0013-LOCAL-PCI-add-glymur_pci_skip-to-work-around-the-sus`
- `0014-security-keys-trusted-keys-include-asn1_decoder.h-fo`
- `0017-LOCAL-COMPAT-A16-Wi-Fi-and-Bluetooth-power-sequencin`
- `0018-LOCAL-COMPAT-A16-display-supply-compatibility`
- `0020-LOCAL-COMPAT-A16-USB-host-and-HDMI-clock-compatibili`
- `0021-LOCAL-COMPAT-A16-retain-CPU-and-LLC-passive-cooling-`
- `0022-LOCAL-COMPAT-A16-retain-board-scoped-SCMI-polling-an`
- `0023-LOCAL-COMPAT-align-QCC2072-endpoint-with-effective-b`
- `0027-LOCAL-keep-HID-ownership-of-Zenbook-keyboard-backlig`
- `0028-regulator-qcom-rpmh-add-PMH0104-camera-LDOs`
- `0029-Snapshot-RC5-source-only-build-push-for-Windows-WSL-continuation`
- `0045-wifi-ath12k-bound-padded-QMI-DMA-allocation-size`
- `0051-Preserve-RPMh-request-lifetime-and-stop-GPU-submission-after-failed-power-votes`
- `0052-Align-bounded-TCS-wait-continuation`
- `0053-Stop-GMU-resume-before-MMIO-when-runtime-power-fails`
- `0054-Add-opt-in-BIOS-312-EC-fan-profile-selection-for-A16`
- `0055-Repair-GMU-lifetime-SoundWire-PM-I2C-DMA-accounting-and-failed-vote-propagation`
- `0056-Reset-successful-DP-configuration-byte-count-before-repeater-training`
- `0057-Keep-pending-I2C-mappings-owned-until-completion-or-termination`
- `0058-dmaengine-qcom-gpi-retire-linked-I2C-command-descriptors`
- `0059-LOCAL-keep-A16-speaker-controllers-runtime-active-for-audio-prevention-baseline`
- `0060-Integrate-16-private-A16-review-source-fixes`
- `0061-Add-approved-A16-hibernation-configuration-fragment`
- `0062-Integrate-tested-A16-Bluetooth-DMA-SoundWire-and-DP-`
- `0063-Preserve-QMP-Type-C-ownership-and-contain-board-tran`
- `0064-Release-GPI-caller-ring-only-on-failures-before-hard`
- `arm64-dts-a16-pmh0104-cooling-maps`
- `arm64-dts-glymur-asus-zenbook-a16-allow-set-time`
- `asoc-qcom-q6apm-dai-period-work`
- `asoc-qcom-q6apm-dai-suspend-capture-reads`
- `asoc-qcom-q6apm-stop-dsp-graphs-on-pcm-suspend`
- `asoc-qcom-x1e80100-suspend-pcms-on-system-suspend`
- `audio-dsp-0002-ASoC-qcom-audioreach-send-topology-cfg-blobs-to-ever`
- `camera-0012-media-camss-limit-Glymur-to-the-validated-nonsecure-`
- `camera-0014-arm64-dts-glymur-asus-zenbook-a16-rgb-ir`
- `drm-msm-dp-report-enable-failure`
- `drm-msm-dp-skip-link-teardown-after-failed-enable`
- `k74-jack-0020`
- `k74-jack-0022`
- `k74-jack-0023`
- `k74-jack-0024`
- `k74-jack-0025`
- `k74-jack-0026`
- `k74-jack-0027`
- `msm-dp-edp-link-training-retry`
- `msm-dp-lab-lt-fault-injection`
- `phy-qcom-edp-v8-firmware-end-values`
- `phy-qcom-qmp-combo-dp-poweron-diag`
- `rtc-qcom-glink-tad-wake-alarm`
- `rtc-qcom-glink-use-soccp-gio-time`
- `swr-driver-0001-soundwire-qcom-acknowledge-interrupts-before-handlin`
- `swr-driver-0002-soundwire-qcom-count-ALERT-peripherals-as-online-for`
- `swr-driver-0003-ASoC-soc-core-leave-trigger-muted-DAIs-muted-on-syst`
- `swr-driver-0004-soundwire-qcom-recover-the-bus-when-auto-enumeration`
- `swr-driver-0005-soundwire-qcom-don-t-handle-interrupts-without-the-i`
- `swr-driver-0006-soundwire-qcom-stop-clash-recovery-across-system-sle`
- `swr-driver-0007-soundwire-qcom-recover-the-bus-while-a-stream-is-ope`
- `swr-driver-0008-soundwire-qcom-factor-the-bus-recovery-out-of-the-wo`
- `swr-driver-0009-soundwire-qcom-check-the-bus-on-system-resume-before`
- `swr-driver-0010-ASoC-qcom-sdw-disable-the-SoundWire-stream-across-sy`
- `swr-driver-0011-soundwire-qcom-recover-a-bus-that-faults-under-a-run`
- `swr-driver-0013-soundwire-qcom-don-t-let-a-no-ALERT-status-read-hide`
