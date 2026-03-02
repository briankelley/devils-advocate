# Self-Hosted / Enterprise Deployment

dvad supports fully internal deployments with no external API calls. This guide
covers private endpoint configuration, cost controls, and air-gapped operation.

## Internal Endpoints via `api_base`

Every model in `models.yaml` accepts an `api_base` field. Point it at any
OpenAI-compatible endpoint: Ollama, vLLM, llama.cpp server, Azure OpenAI, or
your own inference gateway.

```yaml
models:
  internal-llama:
    provider: openai
    model_id: llama-3.3-70b
    api_key_env: INTERNAL_API_KEY
    api_base: https://inference.internal.corp/v1
    context_window: 128000
    max_out_stated: 16384
    max_out_configured: 16384
    cost_per_1k_input: 0
    cost_per_1k_output: 0
    timeout: 300
```

For Anthropic-provider models behind a proxy, set `api_base` to your proxy URL.
The provider field controls which request format dvad uses (Anthropic Messages
API vs. OpenAI Chat Completions).

## Cost Budgets with `--max-cost`

Every review accepts a cost ceiling:

```bash
dvad review --mode plan --project myproject --input plan.md --max-cost 1.50
```

When the running total reaches 80% of the limit, dvad logs a warning. At 100%
the review aborts gracefully, saving all completed work to the ledger.

In the GUI, set the "Max Cost" field on the dashboard before starting a review.

## Air-Gapped Configuration

To guarantee no external network calls:

1. Configure only models whose `api_base` points at internal infrastructure.
2. Do not include any external provider API keys in `.env`.
3. Set `cost_per_1k_input` and `cost_per_1k_output` to `0` for self-hosted
   models where metering is not relevant.

dvad never phones home; all network calls go exclusively to the `api_base` URLs
you configure.

## Provider Allowlisting

Simply omit providers you do not want. If your `models.yaml` only contains
entries with `api_base: https://inference.internal.corp/v1`, dvad will never
contact any external service.

## Ollama Example

```yaml
models:
  ollama-qwen:
    provider: openai
    model_id: qwen2.5-coder:32b
    api_key_env: OLLAMA_DUMMY_KEY     # Ollama ignores this, but the field is required
    api_base: http://localhost:11434/v1
    context_window: 32768
    max_out_stated: 8192
    max_out_configured: 8192
    cost_per_1k_input: 0
    cost_per_1k_output: 0
    timeout: 600
```

Set `OLLAMA_DUMMY_KEY=unused` in your `.env` file. Ollama does not authenticate
but dvad requires the env var to be present.

## vLLM Example

```yaml
models:
  vllm-mixtral:
    provider: openai
    model_id: mistralai/Mixtral-8x7B-Instruct-v0.1
    api_key_env: VLLM_API_KEY
    api_base: http://gpu-cluster:8000/v1
    context_window: 32768
    max_out_stated: 8192
    max_out_configured: 8192
    cost_per_1k_input: 0
    cost_per_1k_output: 0
    timeout: 600
```
