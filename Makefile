PY ?= python3

.PHONY: run demo check test ui-check

run:     ## run from this folder
	$(PY) -m desk_phone

demo:    ## try the call card with a pretend phone
	$(PY) -m desk_phone --demo

check:   ## is everything in place for calls?
	$(PY) -m desk_phone --check

test:    ## tests, in their own D-Bus session (needs pytest; see README, "Working on it")
	dbus-run-session -- $(PY) -m pytest -q tests

ui-check:  ## the recent-calls list in the real card, on an off-screen display (needs xvfb-run)
	xvfb-run -a dbus-run-session -- $(PY) tests/ui_check.py
