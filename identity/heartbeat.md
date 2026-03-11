# Heartbeat

<!-- ================================================================== -->
<!-- STRUCTURE-LOCKED FILE                                               -->
<!-- The AI agent may ONLY append items to the "Scheduled Behaviors"    -->
<!-- list when registering new scheduled tasks. It MUST NOT modify the  -->
<!-- file structure, section headers, or "Standing Orders" content.     -->
<!-- The overall structure and standing orders are set by the user.     -->
<!-- If you are an AI: only append to "Scheduled Behaviors". Do not    -->
<!-- rewrite, restructure, or delete anything else in this file.       -->
<!-- ================================================================== -->

Proactive behavior definitions. Define what your agent should do autonomously.
This file is populated during onboarding based on your preferences.

Scheduled tasks are registered via `goose schedule` and use recipe files in /data/recipes/.
To view active schedules: `goose schedule list`
To add a new one: `goose schedule add --schedule-id <id> --cron "<expr>" --recipe-source /data/recipes/<name>.yaml`

## Standing Orders

(filled in after onboarding)

## Scheduled Behaviors

(filled in after onboarding, based on what you tell the agent you want help with)
