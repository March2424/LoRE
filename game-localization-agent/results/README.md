# Re-scored supplied translations

These scores evaluate the revised hypothesis files supplied after the original evaluation. See revision.json for changed occurrence IDs, input hashes and the audited line repair. The original test set is used for all metrics. Historical status is kept for unchanged rows. For revised rows, an available non-empty hypothesis is marked complete for scoring, without asserting a new model execution.

File layout follows dataset/model/method/direction/target-language.document-id. records.json preserves unescaped text and individual scores. scores.json stores aggregate scores and the current dCOMET or LTCR coverage. Plain hypothesis files use escaped newlines.
