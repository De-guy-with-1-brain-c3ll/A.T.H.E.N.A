# Focused source reading

The existing Beautiful Soup parser now keeps linked cards, heading-scoped
paragraphs and table rows separate. The reader and browser accept `query`, a
short exact subject from the user request. Unmatched sections are excluded;
missing information must be retrieved through relevant links or another source,
not reconstructed from a different section. A focus match is not fact verification.

Links are ranked before the output limit, and navigation links remain available
even when navigation text is removed. Source URLs and explicit section boundaries
are returned with the extracted text. Section metadata does not duplicate the
body in the model prompt. Full-page reads without a query remain supported.

Read-only responses are cached per reader for 60 seconds, at most eight entries.
`refresh: true` bypasses the cache. The original retrieval timestamp remains
visible; a cache hit is labelled. The desktop inspector records these tool outputs.
No extra external service, API key or extraction-model call is required.

Safety checks on public URLs and redirects remain unchanged. JavaScript-only
content still requires the isolated browser. HTML without useful structure is
labelled `unsegmented`, not presented as reliably segmented evidence.
