# User Schema

AGENT-EVOLVING file. Written during onboarding, then GROWS as the agent learns about the user.

## Rules

- Updates MUST be ADDITIVE. Never full-rewrite the file.
- Add new facts under the right section. Keep it terse.
- Target: 400-1000 words populated. Hard max: 2000 words.
- Preserve all section headers even if empty.

## Sections

| Section | Seeded at | Growth | What goes here |
|---------|-----------|--------|----------------|
| Basics | onboarding | updated if changes | name, timezone, occupation/role/company, pronouns |
| Work Context | onboarding | GROWS | current role details, team, responsibilities, active projects (name, status, one-line context), tech stack, work hours, meeting patterns |
| Communication Preferences | onboarding | refined | preferred response style, topics for depth vs brevity, pet peeves |
| Interests & Context | never | GROWS | hobbies, side projects, curiosities, media preferences, recurring topics (add when user shares unprompted) |
| People | never | GROWS | key contacts, their roles, relationship to user. e.g. "Sarah - cofounder, handles product" |
| Patterns & Habits | never | GROWS | observed behavioral patterns (not what they told you, what you noticed): when active, task types requested most, how they phrase requests, stress signals |
| Preferences (Observed) | never | GROWS | specific preferences from interactions: tool preferences, format preferences, domain-specific preferences, scheduling preferences |
| Important Context | never | GROWS and AGES | time-sensitive facts, "remember this" items, life events, deadlines, ongoing situations. review periodically and remove stale entries |
