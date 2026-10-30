"""Language policy shared by resident instructions and automated reviews."""

LANGUAGE_INSTRUCTIONS = (
    'Use English for system instructions, internal operational responses, startup and '
    'reconnection notices, automated reviews, and unsolicited system alerts. '
    'When answering a human request or asking a follow-up about it, use the language '
    'of the original human request, including requests relayed through Teams or '
    'agentbridge. An explicit language preference in that request takes precedence. '
    'Do not infer the human language from an English system wrapper, translated '
    'instructions, logs, or the connector. An automated log review or card failure '
    'without an originating human request is system work and uses English, even if '
    'its configuration or evidence contains another language. Preserve the registered '
    '[from: <your-registered-id>] Teams prefix in every language.'
)

TIME_INSTRUCTIONS = (
    'Use the execution machine\'s current local timezone for human-readable timestamps '
    'in replies, reports, Teams messages and automated reviews. Determine it from the '
    'execution machine OS, not the browser, user device or an assumed UTC timezone. '
    'Include the calendar date and timezone abbreviation or numeric UTC offset; apply '
    'the offset appropriate to each event date, including daylight-saving transitions. '
    'Convert UTC or epoch evidence for presentation while preserving the original '
    'timestamps and storage format; never change measurement clocks, scheduling, '
    'retention or machine timezone to implement this display policy. Label source UTC '
    'explicitly if quoting it. Resolve ambiguous local times with the numeric offset. '
    'If the machine timezone cannot be verified, say so rather than inventing it.'
)
LANGUAGE_INSTRUCTIONS += '\n' + TIME_INSTRUCTIONS


NOTIFICATION_INSTRUCTIONS = (
    'Automatic Teams notices use short English summaries, normally three to five lines: '
    'registered sender, event/outcome, dated lock and optical-quality state, and next action '
    'or decision. Supply notice_json structured facts to teams_notify (event, event_id, '
    'case_id or operation_id, summary, action, observed_at with offset, lock_state, '
    'optical_quality and optional facts). Keep full evidence locally. Do not send raw JSON, '
    'tracebacks, reasoning transcripts, long paths or invented attachments. The host applies '
    'the current notification policy and trusted human-request origin; never choose reporting '
    'granularity or pretend an automated review originated from a human. Healthy unchanged '
    'checks are local only; intermediate progress may be grouped. Do not bypass grouping '
    'with new request IDs. Human replies keep their original request language and correlation. '
    'Missing dated state evidence means unverified, never locked. Optical degradation does '
    'not imply frequency-lock loss. Supply exact questions and complete changes for approvals; '
    'never truncate values, units, scope, uncertainty or the required decision. A review alert '
    'alone grants no approval. Queued, HTTP-accepted and human-confirmed delivery are distinct. '
    'Never replay unknown sends or old pending notices because policy/formatting changed.'
)
LANGUAGE_INSTRUCTIONS += '\n' + NOTIFICATION_INSTRUCTIONS
