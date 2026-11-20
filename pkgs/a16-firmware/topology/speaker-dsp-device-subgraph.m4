# SPDX-License-Identifier: BSD-3-Clause
# Based on audioreach/subgraph-device-codec-dma-playback.m4 (Linaro, 2023).
#
# A16 speakers (WSA_CODEC_DMA_RX_0) with the speaker chain on the ADSP
# (snapx2ee-bringup, bead ids; docs/speaker-protection-research.md):
#
#  ____________________________________________________________________
# |                       Sub Graph (one container)                    |
# | [LOG] -> [MFC 4ch Q27] -> [MSIIR] -> [SAL limiter] -> [CODEC DMA] |
# |____________________________________________________________________|
#
# MFC     stream FL -> FL (woofer L) and LB (tweeter L), FR -> FR (woofer R)
#         and RB (tweeter R); everything else dropped. Output 24-bit (Q27),
#         so the filters have headroom above full scale.
# MSIIR   woofers: high shelf, high-pass, LR4 low-pass; tweeters: high shelf,
#         LR4 high-pass; per-pair pregain.
# SAL     limiter at the ceiling, then 16 bits to the codec DMA.
#
# The cfg words come from speaker-dsp-blobs.m4, generated at build time by
# speaker-dsp-blobs.py from the NixOS speaker options. The kernel sends each
# module's cfg after its media format; a rejected blob fails the stream.
include(`audioreach/audioreach.m4') dnl
include(`util/pcm.m4') dnl
include(`audioreach/subgraph.m4') dnl
include(`audioreach/container.m4') dnl
include(`audioreach/module_log.m4') dnl
include(`audioreach/module_codec_dma.m4') dnl
include(`module_cfg.m4') dnl
include(`speaker-dsp-blobs.m4') dnl
include(`util/mixer.m4') dnl
dnl
undefine(`CDC_DMA_MODULE_IID') dnl
undefine(`LOG_MODULE_IID') dnl
undefine(`MFC_MODULE_IID') dnl
undefine(`MSIIR_MODULE_IID') dnl
undefine(`SAL_MODULE_IID') dnl
undefine(`SG_INDEX') dnl
undefine(`CONTAINER_INDEX') dnl
undefine(`MOD_INDEX') dnl

undefine(`DEVICE_PERF_MODE') dnl
undefine(`DEVICE_DIRECTION') dnl
undefine(`CONT_SENARIO_ID') dnl
undefine(`CONT_CAP') dnl
undefine(`CONT_POSITION') dnl
undefine(`DEVICE_DOMAIN_ID') dnl
undefine(`CONT_STACK_SIZE') dnl

define(`PCM_PERF_MODE', APM_SG_PERF_MODE_LOW_LATENCY) dnl'
define(`PCM_DIRECTION', APM_SUB_GRAPH_DIRECTION_RX) dnl'
define(`DEVICE_PERF_MODE', APM_SG_PERF_MODE_LOW_LATENCY) dnl'
define(`DEVICE_DIRECTION', APM_SUB_GRAPH_DIRECTION_TX) dnl'
define(`CONT_SENARIO_ID', APM_SUB_GRAPH_SID_AUDIO_PLAYBACK) dnl'
define(`CONT_CAP', `ifelse(LPASS_VER_COMPAT, 110000, APM_CONTAINER_TYPE_ID_GC, APM_CONTAINER_CAP_ID_EP)') dnl'
define(`CONT_POSITION', APM_CONT_GRAPH_POS_STREAM)dnl'
define(`DEVICE_DOMAIN_ID', ifdef(`AR_SELECTED_PROC_DOMAIN_ID', `AR_SELECTED_PROC_DOMAIN_ID', `APM_PROC_DOMAIN_ID_ADSP')) dnl'
define(`CONT_STACK_SIZE', 8192) dnl'
dnl
dnl Same instance ids as upstream for LOG, MFC and CODEC DMA; the new modules
dnl follow (the next subgraph's ids start at 0x6180).
define(`LOG_MODULE_IID', MOD_IID_START) dnl
define(`MFC_MODULE_IID', eval(MOD_IID_START + 1)) dnl
define(`CDC_DMA_MODULE_IID', eval(MOD_IID_START + 2)) dnl
define(`MSIIR_MODULE_IID', eval(MOD_IID_START + 3)) dnl
define(`SAL_MODULE_IID', eval(MOD_IID_START + 4)) dnl
define(`SG_INDEX', 1) dnl
define(`CONTAINER_INDEX', 1) dnl
define(`MOD_INDEX', 1) dnl
dnl
dnl DEVICE_PLAYBACK_ROUTE(stream-index, dai-id)
define(`DEVICE_PLAYBACK_ROUTE',
`'
`SectionGraph."NAME_PREFIX.$1 $2 Graph" {'
`        index STR($1)'
`        lines ['
`                "NAME_PREFIX.logger$1, , $2 Audio Mixer"'
`                "NAME_PREFIX.mfc$1, , NAME_PREFIX.logger$1"'
`                "NAME_PREFIX.msiir$1, , NAME_PREFIX.mfc$1"'
`                "NAME_PREFIX.sal$1, , NAME_PREFIX.msiir$1"'
`                "NAME_PREFIX.codec_dma_rx$1, , NAME_PREFIX.sal$1"'
`                "$2 Playback, , NAME_PREFIX.codec_dma_rx$1"'
`        ]'
`}')


AR_SUBGRAPH(SG_INDEX, DEVICE_DAI_ID, SG_IID_START, DEVICE_PERF_MODE, DEVICE_DIRECTION, CONT_SENARIO_ID)
AR_CONTAINER(CONTAINER_INDEX, CONT_IID_START, CONT_CAP, CONT_STACK_SIZE, CONT_POSITION, DEVICE_DOMAIN_ID)
AR_MODULE_LOG(MOD_INDEX, SG_INDEX, CONTAINER_INDEX, 		LOG_MODULE_IID,     1, 1, 1, 2, 0x000019ab, 1, 0, MFC_MODULE_IID)
A16_AR_MODULE_CFG(mfc, MODULE_ID_MFC, `A16_SPK_MFC_CFG_WORDS', MOD_INDEX, SG_INDEX, CONTAINER_INDEX, MFC_MODULE_IID, 1, 1, 1, 2, MSIIR_MODULE_IID)
A16_AR_MODULE_CFG(msiir, MODULE_ID_MSIIR, `A16_SPK_MSIIR_CFG_WORDS', MOD_INDEX, SG_INDEX, CONTAINER_INDEX, MSIIR_MODULE_IID, 1, 1, 1, 2, SAL_MODULE_IID)
A16_AR_MODULE_CFG(sal, MODULE_ID_SAL, `A16_SPK_SAL_CFG_WORDS', MOD_INDEX, SG_INDEX, CONTAINER_INDEX, SAL_MODULE_IID, 1, 1, 1, 2, CDC_DMA_MODULE_IID)
AR_MODULE_CODEC_DMA_RX(MOD_INDEX, SG_INDEX, CONTAINER_INDEX, CDC_DMA_MODULE_IID, 1, 0, 1, 0, DEVICE_INTF_TYPE, DEVICE_INTF_INDEX, DEVICE_DATA_FORMAT, `DEVICE_NAME', NONE_IID)
DEVICE_PLAYBACK_ROUTE(MOD_INDEX, `DEVICE_NAME')
