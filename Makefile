.PHONY: help install-hooks check-hooks uninstall-hooks

# =============================================================================
# Makefile — repository bootstrap
# =============================================================================
# This Makefile exists for one reason: make the local git hooks
# (`commit-msg`, `pre-push`) impossible to forget. Run `make install-hooks`
# once after every fresh clone. It also self-checks (`make check-hooks`)
# and is reversible (`make uninstall-hooks`).
#
# Why a Makefile: it's the one build tool every Unix-ish developer
# already has, it has no runtime dependencies, and it makes the bootstrap
# a single memorable word (`make install-hooks`) instead of an
# error-prone `git config ...` line that the developer might forget on
# their next clone.

GITHOOKS_DIR := .githooks
HOOK_NAMES    := commit-msg pre-push

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install-hooks: ## Install the repo's git hooks (run once per clone)
	@echo "Installing git hooks from $(GITHOOKS_DIR)/ into .git/hooks/"
	@mkdir -p .git/hooks
	@ok=1; \
	for hook in $(HOOK_NAMES); do \
	    src="$(GITHOOKS_DIR)/$$hook"; \
	    dst=".git/hooks/$$hook"; \
	    if [[ ! -f "$$src" ]]; then \
	        echo "  [SKIP] $$src not found"; \
	        continue; \
	    fi; \
	    if [[ -e "$$dst" && ! -f "$$dst.sample" && "$$dst" -ef "$$src" ]]; then \
	        echo "  [OK]   $$dst already linked to $$src"; \
	        continue; \
	    fi; \
	    if [[ -e "$$dst" && ! "$$dst" -ef "$$src" ]]; then \
	        echo "  [BAK]  Backing up existing $$dst -> $$dst.local"; \
	        mv "$$dst" "$$dst.local"; \
	    fi; \
	    chmod +x "$$src"; \
	    if ln -sf "../../$$src" "$$dst"; then \
	        echo "  [OK]   Linked $$dst -> $$src"; \
	    else \
	        echo "  [ERR]  Could not link $$dst"; \
	        ok=0; \
	    fi; \
	done; \
	if [[ $$ok -eq 0 ]]; then exit 1; fi
	@echo
	@echo "Verifying VERSION is in sync..."
	@if ./version_bump.sh --check; then \
	    echo "  [OK]   VERSION matches the implied bump."; \
	else \
	    echo "  [WARN] VERSION is out of sync. Run: ./version_bump.sh"; \
	fi

check-hooks: ## Verify the hooks are installed and executable
	@echo "Checking git hooks..."
	@for hook in $(HOOK_NAMES); do \
	    dst=".git/hooks/$$hook"; \
	    src="$(GITHOOKS_DIR)/$$hook"; \
	    if [[ ! -e "$$dst" ]]; then \
	        echo "  [MISS] $$dst is not installed. Run: make install-hooks"; \
	        exit 1; \
	    fi; \
	    if [[ ! -x "$$dst" ]]; then \
	        echo "  [MODE] $$dst is not executable. Fix: chmod +x $$dst"; \
	        exit 1; \
	    fi; \
	    echo "  [OK]   $$dst"; \
	done

uninstall-hooks: ## Remove the installed git hooks (restores any backups)
	@echo "Uninstalling git hooks..."
	@for hook in $(HOOK_NAMES); do \
	    dst=".git/hooks/$$hook"; \
	    bak=".git/hooks/$$hook.local"; \
	    if [[ -L "$$dst" ]]; then \
	        rm -f "$$dst"; \
	        echo "  [RM]   $$dst (symlink)"; \
	        if [[ -e "$$bak" ]]; then \
	            mv "$$bak" "$$dst"; \
	            echo "  [RESTORE] $$dst <- $$bak"; \
	        fi; \
	    elif [[ -e "$$dst" ]]; then \
	        echo "  [KEEP] $$dst is not a symlink to $(GITHOOKS_DIR); leaving alone"; \
	    fi; \
	done
