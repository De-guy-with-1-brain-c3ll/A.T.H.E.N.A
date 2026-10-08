# Routine actions and notifications

Requested public searches, sandbox file creation, and artifact transfers to the
configured PC inbox do not need a second conversational approval. ATHENA should
follow through on the requested sequence, clarify only essential missing inputs,
and use actual tool receipts rather than promise success.

Transfers run asynchronously. The initial reply means submitted, not completed.
The status tool/dashboard tracks the same operation until the PC verifies the
file hash and byte count. One completion or failure notification is emitted.
There is no periodic spoken transfer heartbeat; byte progress remains visible
in the dashboard. Terminal and Feishu omit generic timer/submission messages.

The destination remains fixed by trusted configuration on the private network,
with authenticated uploads. Path confinement, credential/hidden-file exclusion,
link checks, file-size limits and file/destination pinning remain enforced.
Downloads, destructive actions and system-changing commands retain existing
approval safeguards. This is not permission to upload arbitrary personal files
or automatically invent unrelated tasks.
