# Competition harness wheels (from Kaggle)

Source dataset: [`metric/gemma-4-developer-agent-wheelhouse`](https://www.kaggle.com/datasets/metric/gemma-4-developer-agent-wheelhouse)  
**Not on public PyPI.** Full set is large (flashinfer/bitsandbytes); this folder keeps the **eval harness** subset.

| Wheel | Role |
| --- | --- |
| `swegemma-0.2.7-py3-none-any.whl` | SWE eval / tools / scoring |
| `adk_submission-0.2.12-py3-none-any.whl` | YAML compile + VllmServer |
| `adk_eval_core-0.1.0-py3-none-any.whl` | sandbox / traces |
| `google_adk-1.36.1-py3-none-any.whl` | Google ADK runtime |

```bash
pip install --no-deps \
  ./swegemma-0.2.7-py3-none-any.whl \
  ./adk_submission-0.2.12-py3-none-any.whl \
  ./adk_eval_core-0.1.0-py3-none-any.whl \
  ./google_adk-1.36.1-py3-none-any.whl
# Remaining transitive deps: use Kaggle image or resolve carefully with network.
```

S3 mirror: `s3://gemma4-dev-agent-data-650825122703/tools/wheels_harness/`
