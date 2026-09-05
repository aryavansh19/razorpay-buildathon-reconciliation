# Convenience targets. Everything here is a thin wrapper around a plain
# `python -m` invocation, so nothing depends on make being installed.
#
# On Windows without make, use .\run.ps1 or the python commands directly.

PYTHON ?= python

.PHONY: help demo walkthrough serve publish sweep policy backends qa ask verify clean

help:
	@echo "demo        Generate a batch, reconcile it, write the report"
	@echo "walkthrough Paced 7-beat terminal demo, for screen recording"
	@echo "serve       Local dashboard with the question box enabled"
	@echo "publish     Write docs/index.html for GitHub Pages"
	@echo "sweep     Reconcile 200 independently generated batches"
	@echo "policy    Measure the inactive-order matching policy both ways"
	@echo "backends  Compare the offline baseline against the hosted model"
	@echo "qa        Grade the Q&A agent against the golden question set"
	@echo "ask       Interactive Q&A over the reconciled run"
	@echo "verify    Strict demo, seed sweep and Q&A golden set; non-zero on defect"
	@echo "clean     Remove generated data and reports"

demo:
	$(PYTHON) -m recon.cli

walkthrough:
	$(PYTHON) -m recon.demo

serve:
	$(PYTHON) -m recon.serve

publish:
	$(PYTHON) -m recon.cli --publish --quiet
	@echo "Wrote docs/index.html"

sweep:
	$(PYTHON) -m recon.evals sweep --runs 200

policy:
	$(PYTHON) -m recon.evals policy --runs 40

backends:
	$(PYTHON) -m recon.evals backends

qa:
	$(PYTHON) -m recon.evals qa

ask:
	$(PYTHON) -m recon.ask

verify:
	$(PYTHON) -m recon.cli --strict --quiet
	$(PYTHON) -m recon.evals sweep --runs 40
	$(PYTHON) -m recon.evals qa

clean:
	rm -rf data reports
