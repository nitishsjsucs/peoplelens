# PeopleLens task runner. Thin wrappers over `pl`, `pl-ops`, `pl-eval` and npm.
# Requires GNU make (3.81 or newer; Xcode CLT ships 3.81). Pass extra
# arguments with ARGS, for example: make bootstrap ARGS="--phase b".

UV ?= uv
NPM ?= npm
ARGS ?=

.PHONY: setup data dbt catalog test dev-up dev-down llm-up llm-down \
	eval-local eval-stub doctor bootstrap deploy smoke eval-prod audit-commit

setup:
	$(UV) sync --frozen
	$(NPM) ci

data:
	$(UV) run pl data generate
	$(UV) run pl data stats

dbt:
	$(UV) run pl dbt --dialect duckdb seed
	$(UV) run pl dbt --dialect duckdb build

catalog:
	$(UV) run pl catalog build

test:
	$(UV) run pytest -q
	$(NPM) test

dev-up:
	$(UV) run pl dev up $(ARGS)

dev-down:
	$(UV) run pl dev down

llm-up:
	$(UV) run pl dev llm-up

llm-down:
	$(UV) run pl dev llm-down

eval-stub:
	$(UV) run pl-eval run --llm stub --assert-safety $(ARGS)

eval-local:
	$(UV) run pl-eval run --llm openai_compat $(ARGS)

doctor:
	$(UV) run pl-ops doctor $(ARGS)

bootstrap:
	$(UV) run pl-ops bootstrap $(ARGS)

deploy:
	$(UV) run pl-ops deploy $(ARGS)

smoke:
	$(UV) run pl-ops smoke $(ARGS)

eval-prod:
	$(UV) run pl-eval run --target prod --llm workers_ai $(ARGS)

# optional production audit trail: R2 staging into Basin Catalog (needs the AUDIT_R2_* keys)
audit-commit:
	$(UV) run pl-ops audit commit $(ARGS)
