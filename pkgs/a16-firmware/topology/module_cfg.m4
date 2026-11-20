# SPDX-License-Identifier: BSD-3-Clause
# A processing module with a static cfg blob (snapx2ee-bringup, bead ids),
# after audioreach/module_mfc.m4 (Linaro, 2023). The blob's SectionData is the
# last item of the widget's private data; the kernel stores it as mod->data
# and sends its parameter records after the module's media format.
include(`util/util.m4') dnl
define(`MODULE_ID_MSIIR', `0x07001014') dnl
define(`MODULE_ID_SAL', `0x07001010') dnl
dnl A16_AR_MODULE_CFG(name, module-id, cfg-words-macro, index, sgidx, container-idx, iid,
dnl	maxip-ports, max-op-ports, src-port, dst-port, dst-iid)
dnl cfg-words-macro(iid as 8 hex digits) expands to the blob's words.
define(`A16_AR_MODULE_CFG',
`'
`SectionVendorTuples."NAME_PREFIX.$1$4_tuples" {'
`        tokens "audioreach_tokens"'
`'
`        tuples."word.u32_data" {'
`                AR_TKN_U32_MODULE_INSTANCE_ID STR($7)'
`                AR_TKN_U32_MODULE_ID STR($2)'
`                AR_TKN_U32_MODULE_MAX_IP_PORTS STR($8)'
`                AR_TKN_U32_MODULE_MAX_OP_PORTS STR($9)'
`                AR_TKN_U32_MODULE_SRC_OP_PORT_ID STR($10)'
`                AR_TKN_U32_MODULE_DST_IN_PORT_ID STR($11)'
`                AR_TKN_U32_MODULE_SRC_INSTANCE_ID STR($7)'
`                AR_TKN_U32_MODULE_DST_INSTANCE_ID STR($12)'
`        }'
`}'
`'
`SectionData."NAME_PREFIX.$1$4_data" {'
`        tuples "NAME_PREFIX.$1$4_tuples"'
`}'
`'
`SectionData."NAME_PREFIX.$1$4_cfg_data" {'
`        words "$3(eval($7,16,8))"'
`}'
`'
`SectionWidget."NAME_PREFIX.$1$4" {'
`        index STR($4)'
`        type "src"'
`        no_pm "true"'
`        subseq "10"'
`        data ['
`                "NAME_PREFIX.sub_graph$5_data"'
`                "NAME_PREFIX.container$6_data"'
`                "NAME_PREFIX.$1$4_data"'
`                "NAME_PREFIX.$1$4_cfg_data"'
`        ]'
`}') dnl
