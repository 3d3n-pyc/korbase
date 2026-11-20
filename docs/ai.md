# For AI assistants

korbase is new and pre-alpha. AI assistants know little about it and may get
its API wrong. Give them the material below.

## The agent skill

The repository has a skill for coding agents at
`.agents/skills/korbase/SKILL.md`. It is a short file that shows how to write
a korbase model and query, with the rules that differ from other ORMs.

The same file ships in the package as `korbase/_skill/SKILL.md`, so an
installed korbase carries it. A test keeps the two copies identical and runs
the skill's examples.

## llms.txt

The docs build also writes `llms.txt`, a short map of these pages for
language models. Once the docs site is public, point the assistant at
`https://3d3n-pyc.github.io/korbase/llms.txt`. Until then, point it at the
files in this repository, as below.

## How to point an assistant at them

- In a coding agent that reads the repository, the agent reads `AGENTS.md`
  on its own. It lists the checks to run and the rules to follow.
- In a chat, paste the contents of `.agents/skills/korbase/SKILL.md` into the
  first message, then ask for the task.
- For a question about one page, paste that page from `docs/`.
- Ask the assistant to check its code against the examples in the skill, and
  to run the tests (`uv run pytest`) before it says the change works.

Tell the assistant the Python version: korbase needs Python 3.10 or later.
