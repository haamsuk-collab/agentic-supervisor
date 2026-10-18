PYTHON ?= /usr/bin/python3.12

.PHONY: test
test:
	$(PYTHON) -m unittest discover -s tests -v
