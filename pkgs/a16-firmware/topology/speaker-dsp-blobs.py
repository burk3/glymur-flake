#!/usr/bin/env python3
"""AudioReach cfg blobs for the A16 speaker chain on the ADSP.

Turns the speaker tuning of modules/zenbook/audio (the same values the
PipeWire filter-chain in graph.nix uses) into topology cfg blobs for the
WSA_CODEC_DMA_RX_0 device subgraph (speaker-dsp-device-subgraph.m4):

  MFC    0x07001015  output media format: 48 kHz, 24-bit (Q27), 4 channels
                     FL LB FR RB, plus channel mixer coefficients that copy
                     the stream's FL to FL and LB, FR to FR and RB. All other
                     input channels are dropped.
  MSIIR  0x07001014  per channel mask: woofers (FL FR) high shelf, high-pass,
                     LR4 low-pass; tweeters (LS RS LB RB) high shelf, LR4
                     high-pass; Q27 pregain per pair; enabled.
  SAL    0x07001010  limiter always on (a single input would bypass it),
                     tuning with the threshold at the limiter ceiling.

Channel types after the device MFC are the back end's slot map
(sound/soc/qcom/x1e80100.c: FL LB FR RB for 4 channels) and the DT routes
slots 0/1 to the WSA macro (WooferLeft, TweeterLeft) and 2/3 to WSA2
(WooferRight, TweeterRight). So FL/FR are the woofers and LB/RB the
tweeters. The tweeter mask also covers LS/RS, so a tweeter channel can
never pass the IIR unfiltered if the map ever comes out as FL FR LS RS.

Coefficients are the RBJ biquads PipeWire's filter-chain computes for
bq_highshelf/bq_lowpass/bq_highpass (spa/plugins/audioconvert/biquad.c),
in double precision, then quantized to MSIIR's Q30 (b0 b1 b2 a1 a2, a0 = 1,
y = b.x - a.y; numerator shift factor 2 = Q30, as in the module's defaults
and the engine's unit-test configs).

Usage:
  speaker-dsp-blobs.py PARAMS.json            m4 macro definitions to stdout
  speaker-dsp-blobs.py --report PARAMS.json   coefficients and the response
                                              of each output, quantized vs
                                              double precision

PARAMS.json (written by pkgs/a16-firmware from the NixOS options):
  {"sampleRate": 48000,
   "highShelf": {"freq": 11500, "q": 0.707, "gainDb": -3} | null,
   "crossoverHz": 3000, "wooferLowpassHz": 3400 | null,
   "wooferHighpassHz": 100 | null,
   "gainDb": 0, "wooferGainDb": 0, "tweeterGainDb": 0,
   "limiter": {"enable": true, "ceilingDb": -1}}
"""

import cmath
import json
import math
import sys

Q = 1 / math.sqrt(2)

# Module and parameter IDs (AudioReach engine API headers).
PARAM_ID_MODULE_ENABLE = 0x08001026
PARAM_ID_MFC_OUTPUT_MEDIA_FORMAT = 0x08001024
PARAM_ID_CHMIXER_COEFF = 0x0800101F
PARAM_ID_MSIIR_TUNING_FILTER_ENABLE = 0x08001020
PARAM_ID_MSIIR_TUNING_FILTER_PREGAIN = 0x08001021
PARAM_ID_MSIIR_TUNING_FILTER_CONFIG_PARAMS = 0x08001022
PARAM_ID_LIMITER_CFG = 0x08001050
PARAM_ID_SAL_LIMITER_ENABLE = 0x0800101E
SAL_LIM_DISABLED, SAL_LIM_ENABLED_IF_REQ, SAL_LIM_ENABLED_ALWAYS = 0, 1, 2
SND_SOC_AR_TPLG_MODULE_CFG_TYPE = 0x01001006

# PCM channel types (q6apm.h).
FL, FR, LS, RS, LB, RB = 1, 2, 4, 5, 8, 9
BE_CHMAP = [FL, LB, FR, RB]  # back-end slot order: WL TL WR TR
WOOFERS = [FL, FR]
TWEETERS = [LS, RS, LB, RB]


def mask(types):
    m = 0
    for t in types:
        assert 1 <= t <= 31
        m |= 1 << t
    return m


def db_to_lin(db):
    return 10 ** (db / 20)


# RBJ biquads exactly as PipeWire computes them (cutoff = f * 2 / rate),
# normalized to a0 = 1: (b0, b1, b2, a1, a2).
def _norm(b0, b1, b2, a0, a1, a2):
    return (b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def lowpass(f, rate, q=Q):
    w0 = math.pi * f * 2 / rate
    alpha = math.sin(w0) / (2 * q)
    k = math.cos(w0)
    return _norm((1 - k) / 2, 1 - k, (1 - k) / 2, 1 + alpha, -2 * k, 1 - alpha)


def highpass(f, rate, q=Q):
    w0 = math.pi * f * 2 / rate
    alpha = math.sin(w0) / (2 * q)
    k = math.cos(w0)
    return _norm((1 + k) / 2, -(1 + k), (1 + k) / 2, 1 + alpha, -2 * k, 1 - alpha)


def highshelf(f, rate, q, gain_db):
    A = 10 ** (gain_db / 40)
    w0 = math.pi * f * 2 / rate
    alpha = math.sin(w0) / (2 * q)
    k = math.cos(w0)
    k2 = 2 * math.sqrt(A) * alpha
    ap, am = A + 1, A - 1
    return _norm(
        A * (ap + am * k + k2),
        -2 * A * (am + ap * k),
        A * (ap + am * k - k2),
        ap - am * k + k2,
        2 * (am - ap * k),
        ap - am * k - k2,
    )


def chains(p):
    """The biquad stages and gain (dB) per output pair, as graph.nix builds them."""
    rate = p["sampleRate"]
    shelf = []
    if p.get("highShelf"):
        s = p["highShelf"]
        shelf = [("highshelf %g Hz Q%g %+g dB" % (s["freq"], s["q"], s["gainDb"]),
                  highshelf(s["freq"], rate, s["q"], s["gainDb"]))]
    xo = p["crossoverHz"]
    lp = p.get("wooferLowpassHz") or xo
    woofer = list(shelf)
    if p.get("wooferHighpassHz"):
        hp = p["wooferHighpassHz"]
        woofer.append(("highpass %g Hz" % hp, highpass(hp, rate)))
    woofer += [("lowpass %g Hz (LR4 1/2)" % lp, lowpass(lp, rate)),
               ("lowpass %g Hz (LR4 2/2)" % lp, lowpass(lp, rate))]
    tweeter = list(shelf) + [("highpass %g Hz (LR4 1/2)" % xo, highpass(xo, rate)),
                             ("highpass %g Hz (LR4 2/2)" % xo, highpass(xo, rate))]
    return [
        ("woofers", WOOFERS, woofer, p["gainDb"] + p["wooferGainDb"]),
        ("tweeters", TWEETERS, tweeter, p["gainDb"] + p["tweeterGainDb"]),
    ]


# MSIIR fixed point: Q30 coefficients; numerator shift 2 means Q(32 - 2).
NUM_SHIFT = 2
Q30 = 1 << 30


def q30(x):
    v = round(x * Q30)
    if not -(1 << 31) <= v < (1 << 31):
        raise SystemExit("coefficient %r does not fit Q30 (|x| < 2)" % x)
    return v


def quantize(bq):
    return tuple(q30(c) for c in bq)


def q27(lin):
    v = round(lin * (1 << 27))
    if not 0 <= v < (1 << 31):
        raise SystemExit("gain %r does not fit Q27" % lin)
    return v


def u32(v):
    return v & 0xFFFFFFFF


def pack16(lo, hi):
    return (lo & 0xFFFF) | ((hi & 0xFFFF) << 16)


def u16_words(values):
    """Little-endian u16 array packed into u32 words, zero padded."""
    v = list(values)
    if len(v) % 2:
        v.append(0)
    return [pack16(v[i], v[i + 1]) for i in range(0, len(v), 2)]


IID = "IID"  # placeholder word, substituted by m4 with the module's instance id


def record(param_id, payload):
    """apm_module_param_data + payload, payload padded to 8 bytes."""
    size = 4 * len(payload)
    words = [IID, param_id, size, 0] + list(payload)
    if len(payload) % 2:
        words.append(0)
    return words


def mfc_records():
    out = [48000, pack16(24, len(BE_CHMAP))] + u16_words(BE_CHMAP)
    tables = []
    for in_map in ([FL, FR, LS, RS], [FL, FR]):
        coeffs = []
        for o in BE_CHMAP:
            src = FL if o in (FL, LB) else FR
            coeffs += [0x4000 if i == src else 0 for i in in_map]
        tables += u16_words([len(BE_CHMAP), len(in_map)] + BE_CHMAP + in_map + coeffs)
    return (record(PARAM_ID_MFC_OUTPUT_MEDIA_FORMAT, out)
            + record(PARAM_ID_CHMIXER_COEFF, [2] + tables))


def msiir_records(p):
    cfg, pregain, enable = [], [], []
    for _, types, stages, gain_db in chains(p):
        m = mask(types)
        cfg += [m, 0, pack16(0, len(stages))]
        for _, bq in stages:
            cfg += [u32(c) for c in quantize(bq)]
        cfg += u16_words([NUM_SHIFT] * len(stages))
        pregain += [m, 0, q27(db_to_lin(gain_db))]
        enable += [m, 0, 1]
    n = len(chains(p))
    return (record(PARAM_ID_MSIIR_TUNING_FILTER_CONFIG_PARAMS, [n] + cfg)
            + record(PARAM_ID_MSIIR_TUNING_FILTER_PREGAIN, [n] + pregain)
            + record(PARAM_ID_MSIIR_TUNING_FILTER_ENABLE, [n] + enable)
            + record(PARAM_ID_MODULE_ENABLE, [1]))


# SAL limiter. PARAM_ID_SAL_LIMITER_ENABLE first: the kernel sends 1
# (Enable_if_req), and capi_sal then bypasses the limiter whenever fewer
# than two inputs are active (capi_sal_check_and_update_lim_bypass_mode:
# "siso to lim -> no accumulation -> bypass"). This SAL has one input, so
# only 2 (Enable_always) makes it limit; measured 2026-10-26 (bead ids):
# with 1, a 0 dBFS tone came out 1:1, no gain reduction. The kernel sends
# its enable before the topology blob, so this record wins.
#
# Then the tuning (param_id_limiter_cfg_t). The SAL here takes Q27 (32-bit
# words) from the MSIIR and outputs 16 bits to the codec DMA (the kernel's
# PARAM_ID_SAL_OUTPUT_CFG), so it runs its limiter in the 16-bit domain
# (lim_qf = min(out, in) = Q15) and passes a set threshold on unshifted
# (the >> 12 applies only to a 16-bit input): thresholds are Q3.15.
# The rest are capi_sal's defaults (gain recovery per zero crossing, 1 ms
# wait, attack/release constants).
def sal_records(p):
    lim = p["limiter"]
    ceiling = lim["ceilingDb"] if lim["enable"] else 0.0
    thresh = round(db_to_lin(ceiling) * 32768)
    mode = SAL_LIM_ENABLED_ALWAYS if lim["enable"] else SAL_LIM_DISABLED
    return record(PARAM_ID_SAL_LIMITER_ENABLE, [mode]) + record(PARAM_ID_LIMITER_CFG, [
        thresh,      # threshold, Q15 linear
        0x0100,      # makeup gain, Q8 (1.0)
        0x7EB8,      # gc, Q15
        33,          # max_wait, Q15 s (1 ms)
        0x0B362C80,  # gain_attack, Q31
        0x01F0D180,  # gain_release, Q31
        0x8000,      # attack_coef, Q15
        0x8000,      # release_coef, Q15
        32768,       # hard threshold, Q15 (0 dBFS)
    ])


def blob_words(records):
    size = 4 * len(records)
    return [size, SND_SOC_AR_TPLG_MODULE_CFG_TYPE, 0, 0] + records


def m4_words(words):
    """Comma-separated words; the IID placeholder becomes 0x`'$1."""
    out = []
    for w in words:
        out.append("0x`'$1" if w == IID else "0x%08x" % w)
    lines = [",".join(out[i:i + 6]) for i in range(0, len(out), 6)]
    return ",\n\t".join(lines)


def emit_m4(p):
    print("dnl Generated by speaker-dsp-blobs.py from the NixOS speaker options;")
    print("dnl do not edit. Each macro takes the module instance id as 8 hex digits.")
    print("dnl Parameters: %s" % json.dumps(p, sort_keys=True))
    for name, recs in (("MFC", mfc_records()), ("MSIIR", msiir_records(p)),
                       ("SAL", sal_records(p))):
        print("define(`A16_SPK_%s_CFG_WORDS', `%s')dnl" % (name, m4_words(blob_words(recs))))


# --- report ---------------------------------------------------------------

def response(stages, gain_lin, f, rate, quant):
    z1 = cmath.exp(-2j * math.pi * f / rate)
    h = gain_lin
    for _, bq in stages:
        if quant:
            bq = tuple(c / Q30 for c in quantize(bq))
        b0, b1, b2, a1, a2 = bq
        h *= (b0 + b1 * z1 + b2 * z1 * z1) / (1 + a1 * z1 + a2 * z1 * z1)
    return h


def report(p):
    rate = p["sampleRate"]
    freqs = [20 * 2 ** (k / 3) for k in range(31)]  # 20 Hz .. ~20 kHz
    worst = 0.0
    for name, types, stages, gain_db in chains(p):
        print("%s (channel mask 0x%x), pregain %+g dB = 0x%08x Q27"
              % (name, mask(types), gain_db, q27(db_to_lin(gain_db))))
        for label, bq in stages:
            print("  %-28s b0 %+.9f b1 %+.9f b2 %+.9f a1 %+.9f a2 %+.9f" % ((label,) + bq))
            print("  %-28s %s" % ("", " ".join("0x%08x" % u32(c) for c in quantize(bq))))
        print("  %8s %10s %10s" % ("Hz", "dB", "Q30-float"))
        g = db_to_lin(gain_db)
        for f in freqs:
            hq = response(stages, g, f, rate, True)
            hf = response(stages, g, f, rate, False)
            dq = 20 * math.log10(abs(hq))
            d = dq - 20 * math.log10(abs(hf))
            worst = max(worst, abs(d))
            print("  %8.0f %+10.3f %+10.6f" % (f, dq, d))
    print("woofer+tweeter sum (per side, complex):")
    for f in freqs:
        hs = sum(response(st, db_to_lin(g), f, rate, True) for _, _, st, g in chains(p))
        print("  %8.0f %+10.3f dB" % (f, 20 * math.log10(abs(hs))))
    print("largest quantization error: %.6f dB" % worst)


def main(argv):
    rep = "--report" in argv
    args = [a for a in argv if a != "--report"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    with open(args[0]) as fh:
        p = json.load(fh)
    if p["sampleRate"] != 48000:
        raise SystemExit("the back end runs at 48 kHz only")
    if rep:
        report(p)
    else:
        emit_m4(p)


if __name__ == "__main__":
    main(sys.argv[1:])
