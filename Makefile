.PHONY: test eval eval-free eval-corpus

test:            ## run the unit and security tests (no API calls)
	python -m pytest -q

eval-corpus:     ## regenerate the evaluation PDFs and question set
	python -m eval.corpus

eval-free:       ## retrieval, ablations and rule-based injection benchmark (no Claude calls)
	python -m eval.run_eval --suite free

eval:            ## full evaluation, including answer quality, trust score and the AI scanner (uses Claude)
	python -m eval.run_eval --suite all
