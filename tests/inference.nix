{ testers, fetchurl }:

let
  # Upstream's tiny test model. Real inference exercises router subprocesses,
  # the service sandbox, and OpenAI responses without a large model download.
  model = fetchurl {
    url = "https://huggingface.co/ggml-org/models/resolve/main/tinyllamas/stories260K.gguf";
    hash = "sha256-Jwy6G9UQn0LQM1D2BAYCRWBGTbFzwOOH2R8EJtO9JW0=";
  };
in
testers.runNixOSTest {
  name = "inference";
  nodes.machine = { pkgs, ... }: {
    imports = [ ../modules/inference.nix ];
    hardware.asus-zenbook-a16.inference = {
      enable = true;
      package = pkgs.llama-cpp;
      hexagon.enable = false;
      apiKeyFile = "/etc/inference-test-api-key";
      modelDefaults = {
        ctx-size = 512;
        sleep-idle-seconds = 5;
        threads = 2;
        n-gpu-layers = 0;
        chat-template = "chatml";
      };
      models = {
        story.path = model;
        alternate = {
          path = model;
          settings.temp = 0.2;
        };
      };
    };
    environment.etc.inference-test-api-key = {
      text = "test-only-key";
      mode = "0600";
    };
    environment.systemPackages = [ pkgs.jq ];
    virtualisation.memorySize = 2048;
    virtualisation.cores = 2;
  };

  testScript = ''
    import json
    import shlex

    machine.wait_for_unit("llama-cpp.service")
    machine.wait_for_open_port(8080)

    def request(path, payload=None, raw=False):
        command = "curl --fail --silent --show-error --compressed --max-time 60 -H 'Authorization: Bearer test-only-key' "
        if payload is not None:
            command += "-H 'Content-Type: application/json' --data " + shlex.quote(json.dumps(payload)) + " "
        result = machine.succeed(command + shlex.quote("http://127.0.0.1:8080" + path))
        return result if raw else json.loads(result)

    def states():
        return {m["id"]: m["status"]["value"] for m in request("/models")["data"]}

    with subtest("loopback router and credential-protected API"):
        machine.succeed("ss -ltn | grep '127.0.0.1:8080'")
        code = machine.succeed("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/v1/models")
        assert code == "401", code
        listed = request("/v1/models")
        assert {m["id"] for m in listed["data"]} == {"story", "alternate"}, listed
        assert states() == {"story": "unloaded", "alternate": "unloaded"}
        html = request("/", raw=True)
        assert "<!doctype html" in html.lower(), html[:1000]

    with subtest("OpenAI chat loads requested model on demand"):
        result = request("/v1/chat/completions", {
            "model": "story",
            "messages": [{"role": "user", "content": "Tell a story."}],
            "max_tokens": 8,
            "temperature": 0,
        })
        assert result["model"] == "story", result
        assert result["choices"][0]["message"]["role"] == "assistant", result
        assert result["usage"]["completion_tokens"] > 0, result

    with subtest("model selection evicts the old instance and supports streaming"):
        stream = request("/v1/completions", {
            "model": "alternate", "prompt": "Once upon a time", "max_tokens": 8, "stream": True,
        }, raw=True)
        assert "data: [DONE]" in stream, stream
        chunks = [json.loads(line[6:]) for line in stream.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        assert any(chunk.get("choices") for chunk in chunks), chunks
        current = states()
        assert current["story"] == "unloaded", current
        assert current["alternate"] in {"loaded", "sleeping"}, current

    with subtest("idle unload and explicit unload keep the router available"):
        machine.wait_until_succeeds("curl -fsS -H 'Authorization: Bearer test-only-key' http://127.0.0.1:8080/models | grep sleeping")
        result = request("/v1/completions", {"model": "alternate", "prompt": "Once", "max_tokens": 2})
        assert result["choices"], result
        assert request("/models/unload", {"model": "alternate"})["success"]
        machine.wait_until_succeeds("curl -fsS -H 'Authorization: Bearer test-only-key' http://127.0.0.1:8080/models | jq -e '.data[] | select(.id == \"alternate\") | .status.value == \"unloaded\"'")
        machine.succeed("systemctl is-active llama-cpp.service")

    with subtest("invalid model returns an API error without killing the router"):
        code = machine.succeed("curl -s -o /tmp/error.json -w '%{http_code}' -H 'Authorization: Bearer test-only-key' -H 'Content-Type: application/json' --data '{\"model\":\"missing\",\"prompt\":\"hello\"}' http://127.0.0.1:8080/v1/completions")
        assert code == "400", code
        assert "error" in json.loads(machine.succeed("cat /tmp/error.json"))
        assert len(request("/v1/models")["data"]) == 2
  '';
}
