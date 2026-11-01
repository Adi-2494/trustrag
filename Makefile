.PHONY: test eval eval-free eval-corpus eval-download-qasper eval-trend

test:            ## run the unit and security tests (no API calls)
	python -m pytest -q

eval-corpus:     ## regenerate the evaluation PDFs and question set
	python -m eval.corpus

eval-free:       ## retrieval, ablations and rule-based injection benchmark (no Claude calls)
	python -m eval.run_eval --suite free

eval:            ## full evaluation, including answer quality, trust score and the AI scanner (uses Claude)
	python -m eval.run_eval --suite all

eval-download-qasper: ## download and verify the Qasper subset (network, human gate; first run: python -m eval.qasper_download --record-hash)
	python -m eval.qasper_download

eval-trend:      ## regenerate eval/results/TRENDS.md from eval/results/history.jsonl (no network)
	python -m eval.trend
