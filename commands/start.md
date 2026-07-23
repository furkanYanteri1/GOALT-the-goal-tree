---
description: Analyze this project and build a GoalT goal tree for it, linking real files and backend artifacts to each goal.
---

Build a GoalT goal tree for the current project.

1. Figure out the absolute path of the project root (the current working directory, unless told otherwise).
2. Explore the codebase before inventing any structure: read the README if one exists, the package manifest (package.json / pyproject.toml / etc.), the top-level folder layout, and -- if present -- database migration files or a schema definition (e.g. a `supabase/migrations` folder, a Prisma schema, a Django models file).
3. From what you actually find, identify the project's real functional areas (e.g. authentication, payments, a specific feature area) -- don't guess generic categories that don't match this codebase.
4. Call `create_tree` with a root_label describing the project and `project_root` set to the absolute path from step 1.
5. Call `add_goal` for each functional area you identified, writing a genuine one-or-two-sentence description of what it does in this specific codebase. Where you can confidently identify the files that implement it, pass them as `related_files` (paths relative to project_root). Where relevant backend artifacts exist (database tables, edge functions, API routes), pass them as `related_backend`.
6. If some goals genuinely depend on or serve more than one parent area, give them multiple parents -- don't force everything into a single-parent tree if the real structure isn't like that.
7. Once the tree is built, call `open_dashboard` and tell the user it's ready, briefly summarizing what you found.

Do not fabricate files or backend artifacts you haven't actually seen -- it's fine, and expected, for some goals to have no related_files yet if you're not confident about the mapping. Leave those empty rather than guessing.
