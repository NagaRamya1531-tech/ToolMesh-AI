# README benchmark figures

Render both SVGs from the committed benchmark exports with Python and uv.

```sh
uv run --no-project --with matplotlib==3.10.7 python docs/charts/render.py
```

The rendering dependency stays outside the application environment. The retrieval figure uses the strategy summary of the [10,000-definition export](../../benchmarks/results/m4-seed42-10000x1000.json.gz). The provider figure sums reported input tokens by strategy across the [corrected M8 export](../../benchmarks/results/m8-openai-corrected.json). The README presents the same values in accessible tables.
