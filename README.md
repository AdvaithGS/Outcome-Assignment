# Document Extraction Service

**Role applied for:** AI/ML Developer

## Run

```bash
cp .env.example .env   # set LLM_BASE_URL and LLM_API_KEY
docker compose up
curl localhost:8080/health
```

_TODO: submit a job, poll it, example request/response._

## Sample-set scores (`tools/validate.py`)

```bash
python tools/validate.py outputs/ --schema-dir sample/schemas --truth-dir sample/sample-data-output-validated
```

| Document | Score | Hallucinations | Misses | Rows |
|---|---|---|---|---|
| _TODO_ | | | | |

## Tests

_TODO_
