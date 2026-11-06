# Outcome take-home: Document Extraction Service

**Roles:** AI/ML Developer, Software Developer (full-time, IIT Kanpur 2026-27 placements)
**Opens:** Saturday 7 November 2026, 12:00 AM IST
**Closes:** Sunday 8 November 2026, 11:59 PM IST
**Sample documents, field lists and answers:** in this package, under `sample/`

If you were shortlisted for both roles, pick **one** before you start. You submit once, for one role. Read section 7 to see what each role is judged on, then decide. State the role in your README and in the submission form.

---

## 1. The task

Build a service that takes a document and a list of fields, and returns the extracted values as JSON.

- **Input:** one file (PDF, DOCX, XLSX or CSV) and one field list (a JSON file with field names and descriptions, see `contract/schema.example.json`). Your service accepts all four file types through the same endpoint; what you do with each inside is up to you.
- **Output:** one result per document in the fixed format described in `contract/README.md`, validated by `tools/validate.py`.
- **Documents:** commercial real estate paperwork. Leases, rent rolls, box score summaries, operating statements. Some are clean digital PDFs, some are scanned, some are spreadsheets with several sheets. Some fields are a single value for the whole document, some are one per row of a table, some have to be computed from what the document says, and some are genuinely not in the document.

The field list is our input to you. It is a starting point, not a contract: rewrite the descriptions, add hints, restructure it, do whatever makes your extraction better. The output field keys must stay the same so we can score them. Nothing about your service may depend on a particular field list existing: the test set includes field lists that are not in the sample.

This is the core of what we build at Outcome. We are not looking for a demo. We are looking for a pipeline that is accurate, cheap to run, and does not fall over when we send it real work.

How you get there is yours: which models, whether you work from text or from page images, whether you extract field by field or all at once, how you check your own work. Any language. Any coding assistant. Any open-source library.

---

## 2. What you get

| Item | Where |
|---|---|
| Field lists, one per document type | `sample/schemas/` |
| Sample documents and their answers | `sample/sample-data-documents/`, `sample/sample-data-output-validated/` |
| The interface contract: endpoints, output format, env vars | `contract/` |
| A validator that checks your output and scores it against the sample answers | `tools/validate.py` |
| An OpenRouter API key with **$10** of credit | by email, personal to you |

The sample set is a **training set**. We score you on a separate, larger set of documents. Some will be simpler than the samples, some harder, and some will come with a field list you have not seen. Your service takes any document and any field list; build for that, not for the specific files.

---

## 3. Models

Use **only** these, through the OpenRouter key we send you:

| Provider | Model id | Note |
|---|---|---|
| Google | `google/gemini-3.8-flash` | fast, cheap, multimodal |
| Google | `google/gemini-3.1-pro-preview` | stronger reasoning |
| Anthropic | `anthropic/claude-haiku-5.5` | fast, cheap |
| Anthropic | `anthropic/claude-sonnet-5.5` | stronger reasoning |
| OpenAI | `openai/gpt-6-luna` | fast, cheap |
| OpenAI | `openai/gpt-6.1-sol` | stronger reasoning |
| Open | `z-ai/glm-5.3-flash` | cheap vision model, good for page images |
| Open | `qwen/qwen3-vl-235b-a22b-instruct` | open vision model |
| Open | `deepseek/deepseek-v3.2` | open text model, strong reasoning for the price |

Rules that disqualify a submission if broken:

- **Any model not on this list, any other AI provider, or any third-party paid service** (a cloud OCR API, a hosted parsing service). We run your code against this exact list on our own key. If it calls anything else, it fails on our side and scores zero.
- **Hardcoded keys or endpoints.** All model calls go through `LLM_BASE_URL` and `LLM_API_KEY` (see `contract/README.md`).

**$10 is the whole budget. There are no top-ups.** It is more than enough for two days of work on the sample set with these models if you spend it with some care. Running the entire sample through the most expensive model on every iteration is a choice, and it is a bad one. Treat the budget the way you would treat a production budget, because that is one of the things we are judging.

Your key is a small, individual key. When you test several documents at once you will hit the provider's rate limits (HTTP 429) and the occasional transient error. That is expected, it is what real providers do, and your service is supposed to cope with it rather than fail the job. Our scoring run uses a larger key, so throttling will be lighter there, but not absent.

---

## 4. Rules

- **Work alone.** Do not share code, prompts, outputs or approaches with other candidates. Anything is acceptable except cheating. We run similarity checks across every submission, and plagiarism in any form means disqualification, no discussion.
- **Commit as you go, in a private repository.** Your git history is required for consideration (see section 5). Real work leaves a trail; a trail we can read is in your favour.
- Coding assistants (Claude Code, Cursor, Copilot, anything) are allowed and expected. Understand what they wrote.
- Any language, any framework, any open-source library.
- Questions go through SPO. We answer genuine blockers (a key that does not work, a broken sample file). We do not answer design questions, "is X allowed" questions already covered here, or requests for hints. If the brief is silent on something, decide, and write the decision down in your DESIGN.md.
- Any clarification we do issue goes to everyone.

---

## 5. What to submit

Work in a **private git repository from the first commit**. When you are done, zip your repository with its `.git` folder included and upload it through the submission form linked in your email, before the deadline. One response per candidate, signed in with your IITK Google account; you can re-upload until the deadline and we read the latest.

```bash
cd <your repo root>
git status            # commit everything first
zip -r <rollno>_<firstname>_<lastname>_<role>.zip . -x 'venv/*' '.venv/*' 'node_modules/*' '__pycache__/*' '.env'
```

Name the zip exactly `<rollno>_<firstname>_<lastname>_<role>.zip`, lowercase, with `role` being `aiml` or `software`, e.g. `210109_aman_khilani_aiml.zip`. Every submission lands in one folder, so a zip we cannot identify from its name is a zip we may not score.

The zip must be under 100 MB, so leave out virtual environments, dependencies, model weights and any data we gave you. A zip of a few tens of MB can take several minutes to upload on a campus connection, so do not start the upload at 11:58 PM. Your git history is part of the submission: **a zip without a `.git` folder, or a repository with a single "final" commit, is not considered.** We read how the work was built, not only what was built.

Your repository must contain:

1. **The service.** `docker compose up` from the repo root starts it on port 8080 and `GET /health` answers within 60 seconds. Nothing else to install, no manual steps.
2. **`README.md`** with: the role you are applying for, how to run it, and your scores on the sample set from `tools/validate.py`.
3. **`DESIGN.md`**, about one page: the decisions you made and why, the tradeoffs you took, which models you used for what and roughly how much of the $10 you spent, what broke and how you handled it, and what you would do with another week. This is the document we read first.
4. **`outputs/`**: your service's result JSON for every sample document, named after the document (`lease_003.pdf` -> `outputs/lease_003.json`), each with a top-level `"schema"` key naming the field list used, so `tools/validate.py outputs/ --schema-dir sample/schemas --truth-dir sample/sample-data-output-validated` runs clean.

The validator must print `VALID` for every output. If it does not, we cannot score you.

Do not commit your API key. Do not commit the sample documents or answers. Both are checked.

---

## 6. How we evaluate

Every submission goes through the same automated run first, then the strongest go to a manual code and design review.

**Gates.** A submission that fails any of these is not scored further:

- Builds and starts from a clean machine with `docker compose up`, `/health` passes.
- Reaches a terminal status with valid output for at least 80% of our test documents. Each document gets **30 minutes** from the moment we submit it, including any time it spends waiting in your queue.
- Uses only the listed models through the env-var gateway.

**Components.** Weights differ by role so you know where to spend your time.

| Component | What we measure | AI/ML | Software |
|---|---|---|---|
| **Accuracy** | Per-field correctness on our test set after normalisation. Harder fields count more. Wrong values cost more than nulls. Correct citations earn a bonus. | 35 | 20 |
| **Cost** | Dollars per document on our run, measured by us, not from your metadata. Cheaper is better, but not at the expense of accuracy. We are looking for the balance, not the floor. | 15 | 5 |
| **Load and latency** | We keep up to **10 documents in flight at a time** and keep submitting. Your service must accept all of them (queue them if you like) and finish each within the time limit. We measure completion rate, p50 and p95 latency, and whether any job is lost or duplicated. The pipeline should scale with compute: give it more workers and it goes faster. Code that cannot scale no matter the compute is what we are looking for here. | 15 | 30 |
| **Design and code** | Manual review of your repository and DESIGN.md against what an experienced engineer would have done. See section 7. | 35 | 45 |

A submission that is exceptional on any one component gets a manual review regardless of its total. Be excellent at something.

---

## 7. Where to focus

The task is the same for both roles. The emphasis is not.

**AI/ML Developer.** We want to see how you think about getting the right answer at the right price.

- The documents have tables, headers, merged cells, multiple sheets, and pages with no text layer. Handle them.
- The field descriptions we give you are a first draft. The candidates who get the best results usually rewrite them.
- Which model you use for which field, and why. Where the cheap model is enough and where it is not.
- How you structure your calls, and what happens when a model returns something unusable.
- How much you trust your own output, and what you do about it.
- Null discipline. A field that is not in the document returns null. A plausible guess is worse than a null.
- Calculated fields are still fields. Get them right.
- An eval loop. You have sample answers and a validator. Show us you used them.

**Software Developer.** We want to see a service that would survive contact with production.

- A real async job model: `POST /jobs` returns immediately, work happens in the background, status is tracked properly, terminal states never change.
- A crashed or restarted worker should not lose a job.
- Submitting the same document and field list twice should not do the work twice.
- The model provider will throttle you and sometimes fail. Jobs should not fail because of it.
- If you implement `X-Organization-Id`, one tenant must never see another's jobs.
- When a job fails, someone should be able to find out why from the logs.
- Tests that would catch a regression in the parts that matter, not just the happy path.
- Accuracy still matters. A beautiful service that extracts the wrong numbers is not a product.

Both roles: write the DESIGN.md as if you were handing the service to a colleague who has to run it on Monday.

---

## 8. Timeline

| When (IST) | What |
|---|---|
| Sat 7 Nov, 12:00 AM | Assignment opens. This package (brief, contract, sample documents with answers, validator) and your API key. |
| Sun 8 Nov, 11:59 PM | Deadline. Your zip must be uploaded through the form by then. |
| Following week | Scoring, code and design review, shortlisting for interviews. |

Good luck. Build something you would be proud to walk us through.

Team Outcome
