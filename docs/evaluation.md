# Evaluation

Each scenario declares a root-cause category, required findings, allowed external specialists, and forbidden conclusions. A runner can score a serialized `InvestigationState` using:

| Dimension | Weight |
| --- | ---: |
| Correct root-cause category | 30 |
| Expected evidence recovered | 20 |
| Appropriate datasource routing | 15 |
| Unsupported causal claims avoided | 15 |
| Appropriate external-agent use | 10 |
| Query and iteration efficiency | 10 |

Negative controls make external-attribution errors expensive: a coincident real event must not override stronger internal evidence. Track SQL count, graph iterations, external calls, elapsed time, and model token usage alongside the quality score.
