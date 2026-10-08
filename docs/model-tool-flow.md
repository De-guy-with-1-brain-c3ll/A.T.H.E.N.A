# Model-driven tool flow

Normal voice, text, dashboard and Feishu requests do not enter the legacy
`handle_user_command` dispatcher. DeepSeek sees a stable catalogue of all
registered tools and uses `select_tools` to load up to six detailed schemas.
Tool selection is not a keyword allowlist. The clock and execution-status
readers have small schemas and are available immediately.

`select_tools` does not execute anything. Actual tool calls still go through
the registry's argument validation, timeouts, operation receipts and approval
checks. Unknown tool names cannot execute. The discovery step adds a model
round trip for less common tools, but avoids sending every full schema on
every greeting. Ordinary turns are bounded to eight rounds; selected coding
work can use twelve. Search remains capped at three distinct queries.

Only a fresh approval/cancellation reply to an existing hub grant uses the
separate permission gate. A plain yes without a grant goes to the model.
Shutdown authorization is recorded from fresh user input, but the model must
call the shutdown tool; a website or model-generated instruction cannot grant
that authorization.

Clock-only replies preserve the successful clock tool result. The default
timezone is ATHENA_TIMEZONE, falling back to Asia/Shanghai. A clock measurement
is refreshed after model generation so a minute/day rollover does not leave
the answer stuck at the earlier measurement. Clock questions still depend on
model intent recognition; time-looking unsupported answers get a correction
pass rather than being spoken unchecked.

Recent operation IDs, labels and states accompany conversation context.
Completion claims without a successful action/status receipt are withheld and
given one repair pass. Submitted background tasks are not completion receipts.
These checks reduce fabrication; they do not guarantee every possible natural
language claim is understood correctly. Tests use both mocked failures and
bounded live model checks. No physical speaker test is required for routing.

The legacy parser remains available for existing development fixtures and
compatibility tests; it is not the live conversation route.
