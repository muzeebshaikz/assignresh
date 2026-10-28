PY ?= python
setup:
	$(PY) -m pip install -r requirements.txt
test:
	$(PY) -m pytest -q
demo: reset
	$(PY) demo.py
reset:
	$(PY) -c "import shutil; shutil.rmtree('data', ignore_errors=True)"
api:
	$(PY) -m uvicorn sync_engine.api:app --port 8000
