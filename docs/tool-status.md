# Verified task status

Every registry tool invocation gets a bounded execution receipt and operation ID.
`check_tool_status` accepts either `tool` (any registered name) or `operation_id`.
Receipts survive interface/service restarts in `ATHENA_DATA_DIR/tool-status.sqlite3`.
Worker forks share the same store. Commands, source code and full URLs are not
stored as request metadata. Results are capped at 700 characters; history at 100.

"Is it finished?" uses recent user context, or the latest actual execution when
no topic is stated. Explicit transfer/download/coding questions remain separate.
Status answers are local: no STT beyond the spoken question, no DeepSeek polling.
Waiting for approval, processing a conversation, execution, and completion are
distinct. Stale running receipts are **unconfirmed**, never reported as finished.

PC transfers require fresh approval and matching SHA256/byte-count receipts.
Approval and transfer share one operation ID. A failed notification cannot turn
a successful transfer into a failed transfer. Workflow creation records submission,
not completion; `background_workflow` status lists actual procedure steps.

These are execution receipts, not continuous provider health checks. For current
VPN/browser/audio state use their existing status/device controls. Completing
`set_alarm` means the alarm was saved, not that it has rung.
