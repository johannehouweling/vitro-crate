# Upstream request (draft)

**Target:** [BerriAI/litellm](https://github.com/BerriAI/litellm) — `model_prices_and_context_window.json`
**Status:** to file

Everything below the line is the issue text.

---

## `azure/gpt-5.6-luna` pricing disagrees with Azure's published page

The entry for `azure/gpt-5.6-luna` (and the `azure/us/` and `azure/eu/` variants)
prices the model well below what Azure publishes.

**In the table:**

```json
"input_cost_per_token":  2.2e-07,   // $0.22 / 1M
"output_cost_per_token": 1.32e-06,  // $1.32 / 1M
"cache_read_input_token_cost": 2.2e-08
```

**On [Azure's pricing page](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)**, GPT-5.6-luna (short context, Data Zone):

| | published | table | |
|---|---:|---:|---:|
| input / 1M | €0.97 | $0.22 | 4.4× |
| output / 1M | €5.80 | $1.32 | 4.4× |
| cached input / 1M | €0.10 | $0.022 | 4.5× |

Two things suggest this is not simply a tier we are reading wrong:

- the gap is **uniform across every field**, cached reads included, which looks
  more like a scale error than a deployment-type difference;
- the out:in ratio is identical in both (~6.0), i.e. it is the same model priced
  on a different basis rather than a different model.

Deployment type may still explain it — Global Standard is cheaper than Data Zone
— but a 4.4× spread seems large for that, and the entry carries no marker saying
which basis it is on. If it is intentionally Global Standard, it would help to
say so, since consumers reading it as "the price of this model" will be out by
4.4× on any other deployment type.

Reported from a downstream tool that shows users a running cost estimate: it read
$0.46 for a session that was billed $2.08.
