{ lib, cfg }:

let
  sp = cfg.speakers;
  lim = sp.limiter;
  q = 0.7071067811865476;

  dbToLinear =
    db:
    let
      z = db * 0.11512925464970229 / 1048576.0;
      e0 = 1.0 + z + z * z / 2.0 + z * z * z / 6.0;
    in
    lib.foldl' (e: _: e * e) e0 (lib.range 1 20);

  biquad = label: name: freq: {
    type = "builtin";
    inherit label name;
    control = {
      Freq = freq * 1.0;
      Q = q;
    };
  };
  shelf = side: {
    type = "builtin";
    label = "bq_highshelf";
    name = "shelf${side}";
    control = {
      Freq = sp.highShelf.freq * 1.0;
      Q = sp.highShelf.q * 1.0;
      Gain = sp.highShelf.gainDb * 1.0;
    };
  };
  gain = name: db: {
    type = "builtin";
    label = "linear";
    inherit name;
    control = {
      Mult = dbToLinear db;
      Add = 0.0;
    };
  };

  source = side: if sp.highShelf == null then "in${side}" else "shelf${side}";
  lpHz = if sp.wooferLowpassHz == null then sp.crossoverHz else sp.wooferLowpassHz;

  chain =
    side: role:
    let
      x = role + side;
      filters =
        if role == "W" then
          lib.optional (sp.wooferHighpassHz != null) (biquad "bq_highpass" "hpW${side}" sp.wooferHighpassHz)
          ++ [
            (biquad "bq_lowpass" "lp${x}a" lpHz)
            (biquad "bq_lowpass" "lp${x}b" lpHz)
          ]
        else
          [
            (biquad "bq_highpass" "hp${x}a" sp.crossoverHz)
            (biquad "bq_highpass" "hp${x}b" sp.crossoverHz)
          ];
      trim = if role == "W" then sp.wooferGainDb else sp.tweeterGainDb;
      nodes = filters ++ [ (gain "gain${x}" (sp.gainDb + trim)) ];
      names = [ (source side) ] ++ map (n: n.name) nodes;
    in
    {
      inherit nodes;
      links = lib.zipListsWith (a: b: {
        output = "${a}:Out";
        input = "${b}:In";
      }) (lib.init names) (lib.tail names);
      output = "gain${x}:Out";
    };

  limiter = role: l: r: {
    node = {
      type = "ladspa";
      name = "lim${role}";
      plugin = "${lim.package}/lib/ladspa/fast_lookahead_limiter_1913.so";
      label = "fastLookaheadLimiter";
      control = {
        "Input gain (dB)" = 0.0;
        "Limit (dB)" = lim.ceilingDb * 1.0;
        "Release time (s)" = lim.releaseSeconds * 1.0;
      };
    };
    links = [
      {
        output = l.output;
        input = "lim${role}:Input 1";
      }
      {
        output = r.output;
        input = "lim${role}:Input 2";
      }
    ];
    outputs = [
      "lim${role}:Output 1"
      "lim${role}:Output 2"
    ];
  };

  wl = chain "L" "W";
  wr = chain "R" "W";
  tl = chain "L" "T";
  tr = chain "R" "T";
  chains = [
    wl
    wr
    tl
    tr
  ];
  limiters = lib.optionals lim.enable [
    (limiter "W" wl wr)
    (limiter "T" tl tr)
  ];

  outputs = if lim.enable then lib.concatMap (l: l.outputs) limiters else map (c: c.output) chains;

  inputNodes =
    lib.concatMap
      (
        side:
        [
          {
            type = "builtin";
            label = "copy";
            name = "in${side}";
          }
        ]
        ++ lib.optional (sp.highShelf != null) (shelf side)
      )
      [
        "L"
        "R"
      ];
  inputLinks = lib.optionals (sp.highShelf != null) (
    map
      (side: {
        output = "in${side}:Out";
        input = "shelf${side}:In";
      })
      [
        "L"
        "R"
      ]
  );
  ch = sp.channels;

  copyNodes =
    map
      (side: {
        type = "builtin";
        label = "copy";
        name = "in${side}";
      })
      [
        "L"
        "R"
      ];
in
{
  "node.description" = sp.description;
  "media.name" = sp.description;
  "filter.graph" =
    if sp.dsp.enable then
      {
        nodes = copyNodes;
        inputs = [
          "inL:In"
          "inR:In"
        ];
        outputs = [
          "inL:Out"
          "inR:Out"
        ];
      }
    else
      {
        nodes = inputNodes ++ lib.concatMap (c: c.nodes) chains ++ map (l: l.node) limiters;
        links = inputLinks ++ lib.concatMap (c: c.links) chains ++ lib.concatMap (l: l.links) limiters;
        inputs = [
          "inL:In"
          "inR:In"
        ];
        inherit outputs;
      };
  "capture.props" = {
    "node.name" = sp.nodeName;
    "node.description" = sp.description;
    "media.class" = "Audio/Sink";
    "audio.channels" = 2;
    "audio.position" = [
      "FL"
      "FR"
    ];
    "priority.session" = sp.priority;
    "device.icon-name" = "audio-speakers";
    "node.virtual" = false;
  };
  "playback.props" = {
    "node.name" = "${sp.nodeName}.output";
    "node.description" =
      if sp.dsp.enable then "${sp.description} (to the DSP)" else "${sp.description} (crossover output)";
    "target.object" = cfg.rawNodeName;
    "node.dont-fallback" = true;
    "node.passive" = true;
  }
  // (
    if sp.dsp.enable then
      {
        "audio.channels" = 2;
        "audio.position" = [
          "FL"
          "FR"
        ];
      }
    else
      {
        "audio.channels" = 4;
        "audio.position" = [
          ch.wooferLeft
          ch.wooferRight
          ch.tweeterLeft
          ch.tweeterRight
        ];
      }
  );
}
