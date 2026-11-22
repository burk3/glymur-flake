// SPDX-License-Identifier: GPL-2.0-only
/*
 * LAB PROTOTYPE, out-of-tree: TPM 2.0 over the Qualcomm "qcom.tz.tpm" QSEE
 * app, for the ASUS Zenbook A16 (Glymur) where QTEE does not expose the TPM
 * as qcomtee service 81.
 *
 * Transport (QUERY_INFO_2 / SEND_COMMAND, CRB-style control area shared via
 * an SHM bridge) is Xilin Wu's tpm_qcom_qsee.c, GPL-2.0-only,
 * github.com/strongtz/linux-radxa-qcom commit eb9f243b7ca5
 * ("tpm: add Qualcomm QSEE TPM transport driver"), as carried by Armbian.
 *
 * Differences, so that it loads into an unmodified running kernel and is
 * staged for safety:
 *  - no aux device from qcom_qseecom: looks up the app id itself and hangs
 *    the chip off a faux device;
 *  - no SCM TPM-type query (needs a non-exported qcom_scm_call());
 *  - stage= gates what it does (default 0):
 *      0: look up the app id, print it, fail the load;
 *      1: also QUERY_INFO_2, print control area / NV size and the iomem
 *         classification of the control area, fail the load (no mapping);
 *      2: also SHM-bridge + map the control area (only if ca_allow= equals
 *         the control area stage 1 printed and it is not System RAM), send
 *         a fixed set of read-only commands raw and log the responses,
 *         stay loaded with no TPM registered (rmmod before the next try);
 *      3: as 2, but register the TPM with the TPM core instead.
 */

#include <linux/types.h>
#include <linux/clk.h>
#include <linux/clk-provider.h>
#include <linux/of.h>
#include <linux/unaligned.h>
#include <linux/ktime.h>
#include <linux/workqueue.h>
#include <asm/byteorder.h>
#include <linux/device.h>
#include <linux/device/faux.h>
#include <linux/err.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/kernel.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/sizes.h>
#include <linux/slab.h>
#include <linux/types.h>

#include <linux/firmware/qcom/qcom_qseecom.h>
#include <linux/firmware/qcom/qcom_scm.h>
#include <linux/firmware/qcom/qcom_tzmem.h>

#include <linux/tpm.h>
#include <linux/tpm_command.h>

/*
 * From drivers/char/tpm/tpm.h, which an out-of-tree build cannot include
 * (the kernel's -dev output carries no drivers/ sources). Exported GPL.
 */
struct tpm_chip *tpmm_chip_alloc(struct device *pdev,
				 const struct tpm_class_ops *ops);
int tpm_chip_register(struct tpm_chip *chip);
void tpm_chip_unregister(struct tpm_chip *chip);

static int stage;
module_param(stage, int, 0444);
MODULE_PARM_DESC(stage, "0: app lookup, 1: +QUERY_INFO_2, 2: +map, raw read-only diag, 3: +register TPM");

static unsigned long long ca_allow;
module_param(ca_allow, ullong, 0444);
MODULE_PARM_DESC(ca_allow, "stage 2+: the control area address stage 1 reported");

#define QTPM_CA_SIZE		(SZ_4K * 3)
#define QTPM_COMMAND_SIZE	TPM_BUFSIZE
#define QTPM_RESPONSE_SIZE	TPM_BUFSIZE
#define QTPM_MAX_TA_DATA_SIZE	SZ_1K

#define QTPM_CMD_QUERY_INFO_2	(0x4000 | 18)
#define QTPM_CMD_SEND_COMMAND	(0x4000 | 8)

struct qtpm_control_area {
	__le32 request;
	__le32 status;
	__le32 cancel;
	__le32 start;
	__le64 interrupt_control;
	__le32 command_size;
	__le64 command;
	__le32 response_size;
	__le64 response;
} __packed;

#define QTPM_CA_CMD_OFF		(sizeof(struct qtpm_control_area))
#define QTPM_CA_STATUS		offsetof(struct qtpm_control_area, status)
#define QTPM_CA_START		offsetof(struct qtpm_control_area, start)
#define QTPM_CA_COMMAND_SIZE	offsetof(struct qtpm_control_area, command_size)
#define QTPM_CA_COMMAND		offsetof(struct qtpm_control_area, command)
#define QTPM_CA_RESPONSE_SIZE	offsetof(struct qtpm_control_area, response_size)
#define QTPM_CA_RESPONSE	offsetof(struct qtpm_control_area, response)

struct qtpm_query_req {
	__le32 command_id;
} __packed;

struct qtpm_query_rsp {
	__le32 nv_store_size;
	__le64 control_area;
} __packed;

struct qtpm_send_req {
	__le32 command_id;
	__le32 input_size;
	u8 input[QTPM_MAX_TA_DATA_SIZE];
} __packed;

struct qtpm_send_rsp {
	__le32 output_size;
	u8 output[QTPM_MAX_TA_DATA_SIZE];
} __packed;

struct qtpm {
	struct device *dev;
	u32 app_id;
	struct qcom_tzmem_pool *mempool;
	struct tpm_chip *chip;
	void __iomem *control_area;
	phys_addr_t control_area_phys;
	u64 bridge;
	struct mutex lock;	/* control area + TA command path */
	struct delayed_work poll;
	u32 last_result;
	unsigned int polls;
};

static unsigned int poll_ms;
module_param(poll_ms, uint, 0444);
MODULE_PARM_DESC(poll_ms, "stage 2: re-send GetTestResult every poll_ms, log changes and slow replies (0: off)");

/*
 * Elimination aid: GCC clock ids (include/dt-bindings/clock/qcom,glymur-gcc.h)
 * to clk_prepare_enable() before the first TPM command, re-enabling clocks
 * the firmware left on and clk_disable_unused() turned off. No rate change.
 */
static int gcc_ids[8];
static int n_gcc_ids;
module_param_array(gcc_ids, int, &n_gcc_ids, 0444);
MODULE_PARM_DESC(gcc_ids, "GCC clock ids to keep enabled while loaded");

static struct clk *qtpm_clks[8];

static void qtpm_put_clks(void)
{
	int i;

	for (i = 0; i < ARRAY_SIZE(qtpm_clks); i++) {
		if (!IS_ERR_OR_NULL(qtpm_clks[i])) {
			clk_disable_unprepare(qtpm_clks[i]);
			clk_put(qtpm_clks[i]);
		}
		qtpm_clks[i] = NULL;
	}
}

static void qtpm_put_clks_action(void *unused)
{
	qtpm_put_clks();
}

static int qtpm_get_clks(struct device *dev)
{
	struct of_phandle_args args = { .args_count = 1 };
	int i, ret;

	if (!n_gcc_ids)
		return 0;

	args.np = of_find_compatible_node(NULL, NULL, "qcom,glymur-gcc");
	if (!args.np)
		return -ENODEV;

	for (i = 0; i < n_gcc_ids; i++) {
		args.args[0] = gcc_ids[i];
		qtpm_clks[i] = of_clk_get_from_provider(&args);
		if (IS_ERR(qtpm_clks[i])) {
			ret = PTR_ERR(qtpm_clks[i]);
			dev_err(dev, "gcc clock %d: %d\n", gcc_ids[i], ret);
			goto err;
		}
		ret = clk_prepare_enable(qtpm_clks[i]);
		if (ret) {
			clk_put(qtpm_clks[i]);
			qtpm_clks[i] = NULL;
			goto err;
		}
		dev_info(dev, "enabled gcc clock %d (%s)\n", gcc_ids[i],
			 __clk_get_name(qtpm_clks[i]));
	}
	of_node_put(args.np);
	return 0;
err:
	of_node_put(args.np);
	qtpm_put_clks();
	return ret;
}

/* stage 2 only: the bound instance, for the poll work and remove(). */
static struct qtpm *qtpm_diag_dev;

static struct faux_device *qtpm_fdev;

static int qtpm_app_send(struct qtpm *q, const void *req, size_t req_size,
			 void *rsp, size_t rsp_size)
{
	size_t rsp_off = ALIGN(req_size, 8);
	size_t size = rsp_off + rsp_size;
	void *buf;
	int ret;

	buf = qcom_tzmem_alloc(q->mempool, size, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	memset(buf, 0, size);
	memcpy(buf, req, req_size);

	ret = qcom_scm_qseecom_app_send(q->app_id, buf, req_size,
					buf + rsp_off, rsp_size);
	if (!ret && rsp_size)
		memcpy(rsp, buf + rsp_off, rsp_size);

	qcom_tzmem_free(buf);
	return ret;
}

static int qtpm_query_info(struct qtpm *q, u32 *nv_size)
{
	struct qtpm_query_req req = {
		.command_id = cpu_to_le32(QTPM_CMD_QUERY_INFO_2),
	};
	struct qtpm_query_rsp rsp = {};
	int ret;

	ret = qtpm_app_send(q, &req, sizeof(req), &rsp, sizeof(rsp));
	if (ret)
		return ret;

	dev_info(q->dev, "QUERY_INFO_2 raw: %*ph\n", (int)sizeof(rsp), &rsp);

	*nv_size = le32_to_cpu(rsp.nv_store_size);
	q->control_area_phys = le64_to_cpu(rsp.control_area);
	if (!q->control_area_phys)
		return -EINVAL;

	return 0;
}

static int qtpm_kick(struct qtpm *q)
{
	struct qtpm_send_req *req;
	size_t req_size = sizeof(*req);
	size_t rsp_off = ALIGN(req_size, 8);
	size_t size = rsp_off + sizeof(struct qtpm_send_rsp);
	void *buf;
	int ret;

	buf = qcom_tzmem_alloc(q->mempool, size, GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	memset(buf, 0, size);
	req = buf;
	req->command_id = cpu_to_le32(QTPM_CMD_SEND_COMMAND);
	req->input_size = 0;

	ret = qcom_scm_qseecom_app_send(q->app_id, buf, req_size, buf + rsp_off,
					sizeof(struct qtpm_send_rsp));

	qcom_tzmem_free(buf);
	return ret;
}

static int qtpm_xfer(struct qtpm *q, u8 *buf, size_t bufsiz, size_t cmd_len)
{
	void __iomem *cmd, *rsp;
	phys_addr_t cmd_phys, rsp_phys;
	__le64 cmd_le, rsp_le;
	struct tpm_header hdr;
	u32 rsp_len;
	int ret;

	if (cmd_len > QTPM_COMMAND_SIZE)
		return -E2BIG;
	if (cmd_len < TPM_HEADER_SIZE)
		return -EINVAL;

	mutex_lock(&q->lock);

	cmd = q->control_area + QTPM_CA_CMD_OFF;
	rsp = cmd + QTPM_COMMAND_SIZE;
	cmd_phys = q->control_area_phys + QTPM_CA_CMD_OFF;
	rsp_phys = cmd_phys + QTPM_COMMAND_SIZE;
	cmd_le = cpu_to_le64(cmd_phys);
	rsp_le = cpu_to_le64(rsp_phys);

	writel(0, q->control_area + QTPM_CA_STATUS);
	memcpy_toio(cmd, buf, cmd_len);
	memset_io(rsp, 0, QTPM_RESPONSE_SIZE);
	writel(QTPM_COMMAND_SIZE, q->control_area + QTPM_CA_COMMAND_SIZE);
	memcpy_toio(q->control_area + QTPM_CA_COMMAND, &cmd_le, sizeof(cmd_le));
	writel(QTPM_RESPONSE_SIZE, q->control_area + QTPM_CA_RESPONSE_SIZE);
	memcpy_toio(q->control_area + QTPM_CA_RESPONSE, &rsp_le, sizeof(rsp_le));
	writel(1, q->control_area + QTPM_CA_START);
	wmb();

	ret = qtpm_kick(q);
	if (ret)
		goto out;
	rmb();

	memcpy_fromio(&hdr, rsp, sizeof(hdr));
	rsp_len = be32_to_cpu(hdr.length);
	if (rsp_len < TPM_HEADER_SIZE || rsp_len > QTPM_RESPONSE_SIZE ||
	    rsp_len > bufsiz) {
		ret = -EIO;
		goto out;
	}

	memcpy_fromio(buf, rsp, rsp_len);
	ret = rsp_len;
out:
	mutex_unlock(&q->lock);
	return ret;
}

static int qtpm_send(struct tpm_chip *chip, u8 *buf, size_t bufsiz,
		     size_t cmd_len)
{
	return qtpm_xfer(dev_get_drvdata(&chip->dev), buf, bufsiz, cmd_len);
}

/*
 * Stage 2: a fixed set of read-only commands, sent raw (no TPM core), with
 * the responses logged. Nothing here changes TPM state.
 */
static const struct {
	const char *name;
	u8 cmd[22];
	u8 len;
} qtpm_diag_cmds[] = {
	{ "GetTestResult",
	  { 0x80, 0x01, 0, 0, 0, 0x0a, 0, 0, 0x01, 0x7c }, 10 },
	{ "GetCapability(TPM_PROPERTIES, PT_FIXED, 64)",
	  { 0x80, 0x01, 0, 0, 0, 0x16, 0, 0, 0x01, 0x7a,
	    0, 0, 0, 0x06, 0, 0, 0x01, 0x00, 0, 0, 0, 0x40 }, 22 },
	{ "GetCapability(TPM_PROPERTIES, PT_VAR, 32)",
	  { 0x80, 0x01, 0, 0, 0, 0x16, 0, 0, 0x01, 0x7a,
	    0, 0, 0, 0x06, 0, 0, 0x02, 0x00, 0, 0, 0, 0x20 }, 22 },
	{ "GetRandom(16)",
	  { 0x80, 0x01, 0, 0, 0, 0x0c, 0, 0, 0x01, 0x7b, 0, 0x10 }, 12 },
};

static void qtpm_diag(struct qtpm *q)
{
	u8 *buf;
	int i, ret;

	buf = kzalloc(TPM_BUFSIZE, GFP_KERNEL);
	if (!buf)
		return;

	memcpy_fromio(buf, q->control_area, 64);
	print_hex_dump(KERN_INFO, "tpm_qsee_lab ca: ", DUMP_PREFIX_OFFSET, 16, 1,
		       buf, 64, false);

	for (i = 0; i < ARRAY_SIZE(qtpm_diag_cmds); i++) {
		memset(buf, 0, TPM_BUFSIZE);
		memcpy(buf, qtpm_diag_cmds[i].cmd, qtpm_diag_cmds[i].len);
		ret = qtpm_xfer(q, buf, TPM_BUFSIZE, qtpm_diag_cmds[i].len);
		dev_info(q->dev, "diag %s: ret %d, ca status %#x start %#x\n",
			 qtpm_diag_cmds[i].name, ret,
			 readl(q->control_area + QTPM_CA_STATUS),
			 readl(q->control_area + QTPM_CA_START));
		if (ret > 0)
			print_hex_dump(KERN_INFO, "tpm_qsee_lab rsp: ",
				       DUMP_PREFIX_OFFSET, 16, 1, buf,
				       min(ret, 512), false);
	}

	kfree(buf);
}

/*
 * GetTestResult every poll_ms: logs the first reply, every change of
 * testResult (bytes 12..15 after the outData size) or response code, and
 * every reply slower than 100 ms, to time when the fTPM drops into failure
 * mode.
 */
static void qtpm_poll_fn(struct work_struct *work)
{
	struct qtpm *q = container_of(to_delayed_work(work), struct qtpm, poll);
	static const u8 cmd[] = { 0x80, 0x01, 0, 0, 0, 0x0a, 0, 0, 0x01, 0x7c };
	u8 buf[64] = {};
	u32 rc, res = 0xffffffff;
	ktime_t t0;
	s64 us;
	int ret;

	memcpy(buf, cmd, sizeof(cmd));
	t0 = ktime_get();
	ret = qtpm_xfer(q, buf, sizeof(buf), sizeof(cmd));
	us = ktime_us_delta(ktime_get(), t0);
	rc = ret >= 10 ? get_unaligned_be32(&buf[6]) : 0xffffffff;
	if (ret >= 12 && rc == 0) {
		u16 n = get_unaligned_be16(&buf[10]);

		if (12 + n + 4 <= ret)
			res = get_unaligned_be32(&buf[12 + n]);
	}
	res = rc ? rc | 0x80000000 : res;
	if (!q->polls || res != q->last_result || us > 100000)
		dev_info(q->dev, "poll %u: ret %d rc %#x testResult %#x, %lld us\n",
			 q->polls, ret, rc, res, us);
	q->last_result = res;
	q->polls++;
	schedule_delayed_work(&q->poll, msecs_to_jiffies(poll_ms));
}

static const struct tpm_class_ops qtpm_ops = {
	.flags = TPM_OPS_AUTO_STARTUP,
	.send = qtpm_send,
};

static void qtpm_bridge_delete(void *data)
{
	struct qtpm *q = data;

	if (q->bridge)
		qcom_tzmem_shm_bridge_delete(q->bridge);
}

static int qtpm_probe(struct faux_device *fdev)
{
	struct device *dev = &fdev->dev;
	struct qcom_tzmem_pool_config pool = {
		.initial_size = SZ_4K,
		.policy = QCOM_TZMEM_POLICY_MULTIPLIER,
		.increment = 2,
		.max_size = SZ_64K,
	};
	struct qtpm *q;
	u32 nv_size = 0;
	int ram, ret;

	if (!qcom_scm_is_available())
		return -EPROBE_DEFER;

	q = devm_kzalloc(dev, sizeof(*q), GFP_KERNEL);
	if (!q)
		return -ENOMEM;
	q->dev = dev;
	mutex_init(&q->lock);

	ret = qtpm_get_clks(dev);
	if (ret)
		return ret;
	ret = devm_add_action_or_reset(dev, qtpm_put_clks_action, NULL);
	if (ret)
		return ret;

	ret = qcom_scm_qseecom_app_get_id("qcom.tz.tpm", &q->app_id);
	if (ret) {
		dev_err(dev, "qcom.tz.tpm lookup failed: %d\n", ret);
		return ret;
	}
	dev_info(dev, "qcom.tz.tpm app id %u (stage %d)\n", q->app_id, stage);
	if (stage < 1)
		return -ENODEV;

	q->mempool = devm_qcom_tzmem_pool_new(dev, &pool);
	if (IS_ERR(q->mempool))
		return PTR_ERR(q->mempool);

	ret = qtpm_query_info(q, &nv_size);
	if (ret) {
		dev_err(dev, "QUERY_INFO_2 failed: %d\n", ret);
		return ret;
	}

	ram = region_intersects(q->control_area_phys, QTPM_CA_SIZE,
				IORESOURCE_SYSTEM_RAM, IORES_DESC_NONE);
	dev_info(dev, "control area %pa (+%#x), nv_store_size %u, system-ram %s\n",
		 &q->control_area_phys, QTPM_CA_SIZE, nv_size,
		 ram == REGION_DISJOINT ? "disjoint" :
		 ram == REGION_INTERSECTS ? "INTERSECTS" : "MIXED");
	if (stage < 2)
		return -ENODEV;

	if (ram != REGION_DISJOINT || !PAGE_ALIGNED(q->control_area_phys) ||
	    ca_allow != q->control_area_phys) {
		dev_err(dev, "refusing to map: ca_allow=%#llx\n", ca_allow);
		return -EPERM;
	}

	ret = qcom_tzmem_shm_bridge_create(q->control_area_phys, QTPM_CA_SIZE,
					   &q->bridge);
	if (ret) {
		dev_err(dev, "SHM bridge for control area failed: %d\n", ret);
		return ret;
	}
	ret = devm_add_action_or_reset(dev, qtpm_bridge_delete, q);
	if (ret)
		return ret;

	q->control_area = devm_ioremap_wc(dev, q->control_area_phys, QTPM_CA_SIZE);
	if (!q->control_area)
		return -ENOMEM;

	if (stage == 2) {
		/* Stays bound (no chip) so an initrd load is not a failed unit. */
		qtpm_diag(q);
		qtpm_diag_dev = q;
		if (poll_ms) {
			INIT_DELAYED_WORK(&q->poll, qtpm_poll_fn);
			schedule_delayed_work(&q->poll, msecs_to_jiffies(poll_ms));
		}
		return 0;
	}

	q->chip = tpmm_chip_alloc(dev, &qtpm_ops);
	if (IS_ERR(q->chip))
		return PTR_ERR(q->chip);
	/*
	 * No hwrng: every command costs the TA ~170 ms of work on the OOB QUP
	 * bus (NV traffic?), and the hwrng core would keep pulling GetRandom.
	 */
	q->chip->flags |= TPM_CHIP_FLAG_TPM2 | TPM_CHIP_FLAG_SYNC |
			  TPM_CHIP_FLAG_HWRNG_DISABLED;
	/* tpmm_chip_alloc() owns the faux device's drvdata (the chip). */
	dev_set_drvdata(&q->chip->dev, q);

	ret = tpm_chip_register(q->chip);
	if (ret) {
		dev_err(dev, "tpm_chip_register failed: %d\n", ret);
		return ret;
	}

	dev_info(dev, "registered %s\n", dev_name(&q->chip->dev));
	return 0;
}

static void qtpm_remove(struct faux_device *fdev)
{
	struct tpm_chip *chip = dev_get_drvdata(&fdev->dev);

	if (qtpm_diag_dev) {
		if (poll_ms)
			cancel_delayed_work_sync(&qtpm_diag_dev->poll);
		qtpm_diag_dev = NULL;
		return;
	}
	if (chip)
		tpm_chip_unregister(chip);
}

static const struct faux_device_ops qtpm_faux_ops = {
	.probe = qtpm_probe,
	.remove = qtpm_remove,
};

static int __init qtpm_init(void)
{
	qtpm_fdev = faux_device_create("tpm_qsee_lab", NULL, &qtpm_faux_ops);
	return qtpm_fdev ? 0 : -ENODEV;
}
module_init(qtpm_init);

static void __exit qtpm_exit(void)
{
	faux_device_destroy(qtpm_fdev);
}
module_exit(qtpm_exit);

MODULE_DESCRIPTION("LAB: TPM over the Qualcomm qcom.tz.tpm QSEE app (staged prototype)");
MODULE_LICENSE("GPL");
