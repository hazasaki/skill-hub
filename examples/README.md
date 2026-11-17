# Original example content

The small skill and prompt in `hub/` were authored for this repository and are
covered by its MIT license. No third-party catalog or host configuration is bundled.

Try `search_skills(query="review")`, `get_skill(name="review-checklist")`,
`search_prompts(query="summary")`, and `get_prompt(name="change-summary")`.

Add your own `hub/skills/<name>/SKILL.md` and optional supporting files, or
`hub/prompts/<name>.md`. Keep the YAML frontmatter shown in the examples; skill
names must match their directory. Local references must exist inside the bundle.
Place a `DISABLED` file in a skill directory to disable it. Restart both services
after editing content so their startup indexes reflect the change.

To use another directory, set `SKILL_HUB_CONTENT_DIR` to a directory containing
`skills/` and `prompts/` before starting Compose. It is mounted read-only. Returned
materialized file paths belong to the container; remote MCP clients receive the
instruction body but cannot access those filesystem paths directly.
