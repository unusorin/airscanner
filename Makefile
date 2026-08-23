-include .env
export

DEPLOY_DIR  ?= ~/airscanner

.PHONY: deploy
deploy:
ifndef DEPLOY_HOST
	$(error DEPLOY_HOST is not set — create a .env file with DEPLOY_HOST=user@host)
endif
	rsync -av --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
		./ $(DEPLOY_HOST):$(DEPLOY_DIR)/
