# Sample set

This is the training set. Our scoring run uses a separate, larger set of the same document kinds.

```
sample/
  README.md
  schemas/            one field list per document type, named <schema_name>.json
    boxscore.json  budget_variance.json  lease_abstraction.json  offering_memorandum.json
    operating_statement_lines.json  rent_roll_units.json
  sample-data-documents/           the files, named <document_id>.<ext>  (8 documents, see manifest.csv)
  sample-data-output-validated/    answer per document, named <document_id>.json
  manifest.csv        document_id, file, schema_name, kind, notes
```

`manifest.csv` tells you which field list goes with which document and what kind of
document it is (PDF, XLSX, CSV, DOCX). Field lists, documents and answers are all here. Each answer file is in the
same format your service must produce (`contract/output.schema.json`) and carries a
top-level `"schema"` key naming its field list.

Check and score your output for the whole set in one go:

```bash
python tools/validate.py outputs/ --schema-dir sample/schemas --truth-dir sample/sample-data-output-validated
```

Notes
- Answers were produced by our production pipeline. If you believe one is wrong, say so
  in your DESIGN.md with the evidence. We will look.
- Field lists are a starting point. Edit the descriptions however you like. Keep the keys.
- Nothing here is customer data. Names, addresses and figures are synthetic.
