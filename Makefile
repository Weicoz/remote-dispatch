SKILL_REPO ?= ../weicoz-skills
SKILL_SRC := skills/weicoz-remote-dispatch/SKILL.md
SKILL_DEST := $(SKILL_REPO)/skills/weicoz-remote-dispatch/SKILL.md

.PHONY: sync-skill check-skill

sync-skill:
	@test -d "$(SKILL_REPO)/.git" || { echo "未找到技能仓库：$(SKILL_REPO)，用 SKILL_REPO=路径 指定" >&2; exit 1; }
	@mkdir -p $(dir $(SKILL_DEST))
	cp $(SKILL_SRC) $(SKILL_DEST)
	rm -rf $(SKILL_REPO)/skills/weicoz-remote-dispatch/references
	@echo "已同步到 $(SKILL_DEST)"

check-skill:
	@diff -u $(SKILL_SRC) $(SKILL_DEST) && echo "技能副本一致"
	@python3 scripts/check_skill_links.py $(SKILL_SRC)
