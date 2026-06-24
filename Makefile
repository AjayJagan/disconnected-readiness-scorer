.PHONY: help
help: ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: skillsaw
skillsaw: ## Run skillsaw linter on skills and plugins
	@echo "Running skillsaw..."
	@if [ -n "$${SKILLSAW_BIN:-}" ]; then \
		"$${SKILLSAW_BIN}"; \
	else \
		uvx skillsaw; \
	fi

.PHONY: skillsaw-fix
skillsaw-fix: ## Auto-fix fixable skillsaw issues
	@echo "Fixing skillsaw issues..."
	@if [ -n "$${SKILLSAW_BIN:-}" ]; then \
		"$${SKILLSAW_BIN}" fix; \
	else \
		uvx skillsaw fix; \
	fi

.PHONY: lint
lint: ## Run all linters
	@$(MAKE) skillsaw

ARCH_ANALYZER_VERSION ?= v0.1.1
ARCH_ANALYZER_REPO := ugiordan/architecture-analyzer
ARCH_ANALYZER_SHA256_darwin_arm64 := 0835020ce26bf00fea889c38fa5afeab6164994caa149beb743876447a9dcec0
ARCH_ANALYZER_SHA256_linux_amd64  := 972f7251657fa3c2748b8218cf6f5d7679c525080caeb8132fdad80862710e72

_OS   := $(shell uname -s | tr '[:upper:]' '[:lower:]')
_ARCH := $(shell uname -m)
_ARCH := $(subst x86_64,amd64,$(_ARCH))
_ARCH := $(subst aarch64,arm64,$(_ARCH))
_BINARY := arch-analyzer-$(_OS)-$(_ARCH)
_EXPECTED := $(ARCH_ANALYZER_SHA256_$(_OS)_$(_ARCH))

.PHONY: install-arch-analyzer
install-arch-analyzer: ## Download arch-analyzer binary to bin/
	@if [ -z "$(_EXPECTED)" ]; then \
		echo "ERROR: no pinned checksum for $(_OS)-$(_ARCH)"; exit 1; \
	fi
	@mkdir -p bin
	@echo "Downloading $(_BINARY) $(ARCH_ANALYZER_VERSION)..."
	@curl -fsSL "https://github.com/$(ARCH_ANALYZER_REPO)/releases/download/$(ARCH_ANALYZER_VERSION)/$(_BINARY)" -o bin/arch-analyzer
	@ACTUAL=$$(sha256sum bin/arch-analyzer | awk '{print $$1}'); \
	if [ "$(_EXPECTED)" = "$$ACTUAL" ]; then \
		chmod +x bin/arch-analyzer; \
		echo "OK: bin/arch-analyzer (sha256:$$ACTUAL)"; \
	else \
		echo "ERROR: checksum mismatch"; \
		echo "  expected: $(_EXPECTED)"; \
		echo "  got:      $$ACTUAL"; \
		rm -f bin/arch-analyzer; exit 1; \
	fi

.DEFAULT_GOAL := help
