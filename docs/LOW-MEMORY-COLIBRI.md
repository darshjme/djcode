# Optional Colibri runtime

DJcode can use an existing [Colibri](https://github.com/JustVugg/colibri) server for supported MoE models. Colibri keeps selected weights resident and streams other experts from storage. DJcode provides resource planning, a guarded foreground launcher and the coding client; it does not reimplement Colibri's inference engine or increase physical RAM.

Nothing is installed or downloaded automatically. Keep an existing hosted provider if you do not want local weights. A model still needs sufficient dense-weight/KV/cache memory and disk space. Less RAM can mean much slower generation. A small context and output budget reduce memory/work, but cannot make every model fit.

## Inspect existing model files

Install Colibri separately using its official instructions and choose compatible model files under their own license. The DJcode installer also provides `djcode-colibri`:

```sh
djcode-colibri plan --launcher /path/to/colibri/c/coli \
  --model-dir /models/existing-converted-model --ram-gb 12 --context 8192

djcode-colibri serve --launcher /path/to/colibri/c/coli \
  --model-dir /models/existing-converted-model --ram-gb 12 \
  --context 8192 --dry-run
```

These commands call the actual upstream planner and doctor; they inspect headers rather than perform language inference. Upstream may cache analysis metadata beside the model. The JSON preserves upstream resource estimates and warnings. A dry run reports `ready_for_coding` and never starts the server. A tiny synthetic header fixture verifies this integration; it is not a working language model or a memory benchmark.

RAM is an explicit engine budget, **not an OS-enforced limit or a fit guarantee**. Leave capacity for the operating system and other programs. CPU is the default. GPU use requires both explicit device IDs and a VRAM budget, for example `--gpu 0 --vram-gb 12`; this example does not establish that a particular model fits a 16 GB GPU. Do not sum shared/unified RAM and VRAM as if they were independent capacity. Inspect Colibri's actual plan for the machine.

## Start and connect

Remove `--dry-run` only when ready to load the existing weights:

```sh
djcode-colibri serve --launcher /path/to/colibri/c/coli \
  --model-dir /models/existing-converted-model --ram-gb 12 \
  --context 8192 --max-tokens 256
```

The helper checks upstream doctor results and refuses models whose family does not advertise native tool support. It starts a foreground server on `127.0.0.1:8000`, uses the quality policy, ignores saved experimental tuning profiles, applies the resource plan, and limits serving to one KV slot and one queued request. It replaces its own process with Colibri; Colibri owns its engine cleanup. Ctrl-C stops that foreground server. Existing servers and DJcode configuration are untouched.

In another terminal:

```sh
djcode-colibri check
DJCODE_COLIBRI_CONTEXT=8192 DJCODE_COLIBRI_MAX_TOKENS=256 \
  djcode --provider colibri --model djcode-colibri
```

Use the exact context/output settings printed by the launcher. If attaching to a separately managed server, `djcode-colibri check --url http://127.0.0.1:8000/v1` lists its model IDs. Set `DJCODE_BASE_URL` and `--model` accordingly. Optional `COLI_API_KEY` is used for bearer authentication; no key is required for the default local setup. Secrets are not printed in the launch command.

The provider retains DJcode's native tool approval, session, cancellation and subagent behavior. A conservative client context estimate includes tool schemas and reserved output; it can reject a request before the server when the configured budget is too small. Token estimation is approximate, and the server's own tokenizer/context checks remain authoritative. Do not silently raise the client context beyond the configured server capacity.

## Verified scope and limitations

Source reviewed at [fd93c41](https://github.com/JustVugg/colibri/tree/fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708): [launcher](https://github.com/JustVugg/colibri/blob/fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708/c/coli), [planner](https://github.com/JustVugg/colibri/blob/fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708/c/resource_plan.py), [doctor](https://github.com/JustVugg/colibri/blob/fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708/c/doctor.py), [family registry](https://github.com/JustVugg/colibri/blob/fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708/c/family_registry.py), and [gateway](https://github.com/JustVugg/colibri/blob/fd93c41aa6ae2c7d1cc1a1e2d6b79dbe6d341708/c/openai_server.py).

At that source revision, some families support chat but reject native tool requests. HTTP `/v1/models` and `/health` do not advertise tool/context capabilities, so model discovery alone cannot prove coding compatibility. The local doctor exposes the authoritative family descriptor used by the guarded launcher. Directly attached servers still return their own errors; DJcode does not conceal an unsupported-tool response with a heuristic fallback.

The gateway queues requests to a limited engine. Parallel DJcode subagents may wait rather than run inference simultaneously. Very slow disk-streaming models can exceed existing DJcode agent deadlines; this integration does not disable those safeguards. Actual large-model RAM usage, throughput, quantization quality, CUDA/Metal/Windows behavior and the user's 16 GB GPU were not benchmarked. No model weights were downloaded for validation.

Colibri's engine and launcher are separately licensed under Apache-2.0 with additional third-party notices. This integration invokes an external installation and vendors no Colibri source, binaries or weights. Preserve upstream licenses/notices if distributing Colibri itself, and follow the chosen model's separate terms. No performance parity or endorsement is implied.
