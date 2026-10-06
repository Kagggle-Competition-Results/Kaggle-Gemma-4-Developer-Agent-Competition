.PHONY: soundness smoke zip submit-check inventory download-meta download

soundness:
	python scripts/soundness_check.py --data data --out results/soundness.csv

smoke:
	python scripts/run_local_eval.py --agent-dir agent --data data --split configs/splits.json:smoke --out-dir traces/smoke --results results/smoke.json

zip:
	python scripts/build_submission.py --agent-dir agent --out submission.zip

inventory:
	python scripts/inventory_data.py --data data --out data/_meta/local_inventory.json

download-meta:
	python scripts/download_competition_data.py --out data --files tasks.jsonl HARNESS_README.md

download:
	python scripts/download_competition_data.py --out data

submit-check: zip
	@echo "Upload submission.zip via Kaggle UI after GATE_P2; log Public score in docs/experiment_log.md"
