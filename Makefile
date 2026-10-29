# Common tasks. `make` lists them.
#
# Registering with agents runs the server one of three ways (SERVER=...):
#   local      uv run from this checkout; picks up code changes without reinstalling (default)
#   installed  the `super-fetch` command from `make tool-install` / `uv tool install`
#   docker     the Docker image from `make docker-build`
#
#   make register-all                 # Claude Code, Codex and OpenCode, from this checkout
#   make register-claude SERVER=docker
#
# Or a server running `serve --http` elsewhere (docker-compose.yml), with its token in the environment:
#   export SUPER_FETCH_HTTP_TOKEN=...
#   make register-all URL=https://fetch.example.com/mcp

NAME ?= super-fetch
SERVER ?= local
IMAGE ?= super-fetch
VOLUME ?= super-fetch-data
URL ?=
DOCKER_RUN = docker run --rm --shm-size 1g -v $(VOLUME):/data

ifeq ($(SERVER),local)
  SERVER_CMD = uv run --directory $(CURDIR) super-fetch
else ifeq ($(SERVER),installed)
  SERVER_CMD = super-fetch
else ifeq ($(SERVER),docker)
  SERVER_CMD = $(DOCKER_RUN) -i $(IMAGE)
else
  $(error SERVER must be local, installed or docker, got '$(SERVER)')
endif

# `$$SUPER_FETCH_HTTP_TOKEN` is expanded by the shell, so the token isn't echoed with the commands.
# Claude Code and OpenCode store it in their config; Codex reads the variable each time it starts.
ifdef URL
  CLAUDE_ADD = --transport http $(NAME) $(URL) --header "Authorization: Bearer $$SUPER_FETCH_HTTP_TOKEN"
  CODEX_ADD = $(NAME) --url $(URL) --bearer-token-env-var SUPER_FETCH_HTTP_TOKEN
  OPENCODE_ADD = $(NAME) --url $(URL) --header "Authorization=Bearer $$SUPER_FETCH_HTTP_TOKEN"
  NEED_TOKEN = @test -n "$$SUPER_FETCH_HTTP_TOKEN" || { echo "Set SUPER_FETCH_HTTP_TOKEN to the server's token first." >&2; exit 1; }
  REGISTERS = $(URL)
else
  CLAUDE_ADD = $(NAME) -- $(SERVER_CMD)
  CODEX_ADD = $(NAME) -- $(SERVER_CMD)
  OPENCODE_ADD = $(NAME) -- $(SERVER_CMD)
  NEED_TOKEN = @:
  REGISTERS = $(SERVER_CMD)
endif

.DEFAULT_GOAL := help
.PHONY: help setup tool-install test test-network lint format bench doctor \
	serve-http docker-build docker-doctor docker-shell \
	register-all register-claude register-codex register-opencode \
	unregister-all unregister-claude unregister-codex unregister-opencode

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Register with SERVER=local (default), installed or docker, or URL=https://.../mcp for a"
	@echo "  remote server (token in SUPER_FETCH_HTTP_TOKEN). Now: $(REGISTERS)"

# --- Development -----------------------------------------------------------------------------

setup: ## Install dependencies and download the browser
	uv sync
	uv run super-fetch install

tool-install: ## Install the `super-fetch` command from this checkout (for SERVER=installed)
	uv tool install --force --reinstall .
	super-fetch install

test: ## Run the tests (no internet needed)
	uv run pytest -q

test-network: ## Run the tests, including those that need the internet
	SUPER_FETCH_NETWORK_TESTS=1 uv run pytest -q

lint: ## Check code style
	uv run ruff check .
	uv run ruff format --check .

format: ## Fix code style
	uv run ruff check --fix .
	uv run ruff format .

bench: ## Fetch the real sites in bench/sites.toml twice (ARGS="--only spa" to narrow)
	uv run python bench/run.py --passes 2 $(ARGS)

serve-http: ## Run the MCP server over HTTP on localhost:8000, from this checkout
	uv run super-fetch serve --http

doctor: ## Check both fetch tiers on this machine
	uv run super-fetch doctor

# --- Docker ----------------------------------------------------------------------------------

docker-build: ## Build the Docker image
	docker build -t $(IMAGE) .

docker-doctor: ## Check both fetch tiers inside the image
	$(DOCKER_RUN) $(IMAGE) doctor

docker-shell: ## Open a shell in the image
	$(DOCKER_RUN) -it --entrypoint bash $(IMAGE)

# --- Agents ----------------------------------------------------------------------------------

register-all: register-claude register-codex register-opencode ## Register with all three agents

register-claude: ## Register with Claude Code (user scope)
	$(NEED_TOKEN)
	-@claude mcp remove --scope user $(NAME) >/dev/null 2>&1
	claude mcp add --scope user $(CLAUDE_ADD)

register-codex: ## Register with Codex
	$(NEED_TOKEN)
	codex mcp add $(CODEX_ADD)
ifdef URL
	@echo "Codex reads SUPER_FETCH_HTTP_TOKEN when it starts: export it in your shell profile."
endif

register-opencode: ## Register with OpenCode (global config)
	$(NEED_TOKEN)
	opencode mcp add --global $(OPENCODE_ADD)

unregister-all: unregister-claude unregister-codex unregister-opencode ## Remove from all three agents

unregister-claude: ## Remove from Claude Code
	claude mcp remove --scope user $(NAME)

unregister-codex: ## Remove from Codex
	codex mcp remove $(NAME)

unregister-opencode: ## Remove from OpenCode (no CLI command for it, so says what to edit)
	@echo "OpenCode has no command to remove a server: delete \"$(NAME)\" under \"mcp\" > \"servers\""
	@echo "in ~/.config/opencode/opencode.json (or opencode.jsonc)."
