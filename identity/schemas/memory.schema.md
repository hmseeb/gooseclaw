# Memory Schema

STRUCTURE-LOCKED file. The agent may update CONTENT within each section, but MUST NOT rename, delete, reorder, or add new section headers.

## Rules

- This file owns FACTS (integrations, tool configs, project status).
- User profile/preferences/people belong in user.md, not here.
- Write facts under the right section. Do not restructure, merge, split, or remove any section headers.

## Sections

| Section | What goes here | What does NOT go here |
|---------|---------------|-----------------------|
| Integrations | connected services, what they do, how they're configured. NO credentials (those go in vault) | user preferences, people |
| Projects | active projects: name, status, key details, technical context | user's relationship to projects (that goes in user.md Work Context) |
| Tools | tool configurations, usage patterns, gotchas learned at runtime | platform-level tool docs (those are in system.md) |
| Lessons Learned | things that went wrong and how to avoid repeating them. promoted from learnings/ when broadly applicable | one-off errors (those stay in learnings/ERRORS.md) |
